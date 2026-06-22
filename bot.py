"""
Capture Bot v2 — one Telegram brain-dump, routed by urgency.

You talk or type. Claude reads each item and picks a lane:

  * call     -> time-critical, must interrupt you. The bot proposes a phone call
               and waits for ONE tap to confirm, then calls you (Twilio) a few
               minutes before, plus a Telegram ping. Also filed for the record.
  * reminder -> tied to a clock time but routine. Auto-scheduled Telegram ping.
  * followup -> must happen today / by a date, no fixed time. Filed with a due
               date; the nudge engine re-pings open ones through the day.
  * task     -> normal. Straight into the Capture Inbox, dealt with later.

Scheduled reminders are persisted to SQLite so they survive restarts/redeploys.

Config is via environment variables — see .env.example.
"""

import os
import re
import json
import uuid
import sqlite3
import logging
import tempfile
import datetime
from zoneinfo import ZoneInfo

import anthropic
from notion_client import Client as Notion
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

try:
    from openai import OpenAI  # voice transcription
except Exception:
    OpenAI = None

try:
    from twilio.rest import Client as TwilioClient
    from twilio.twiml.voice_response import VoiceResponse
except Exception:
    TwilioClient = None
    VoiceResponse = None

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
NOTION_TOKEN = os.environ["NOTION_TOKEN"]
NOTION_DB_ID = os.environ["NOTION_DB_ID"]

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
AUTHORIZED_CHAT_ID = os.environ.get("AUTHORIZED_CHAT_ID")
TZ = ZoneInfo(os.environ.get("TZ", "America/Chicago"))
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

# Twilio (the call tier). If unset, "call" items fall back to a loud Telegram ping.
TWILIO_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_FROM = os.environ.get("TWILIO_FROM_NUMBER")
MY_PHONE = os.environ.get("MY_PHONE_NUMBER")

CALL_LEAD_MINUTES = int(os.environ.get("CALL_LEAD_MINUTES", "5"))
# Times of day to re-surface open, due items (escalation). 24h, comma-separated.
NUDGE_TIMES = os.environ.get("NUDGE_TIMES", "08:00,13:00,16:30")
DB_PATH = os.environ.get("REMINDER_DB_PATH", "reminders.db")

CATEGORIES = ["Work", "Personal", "Katherine"]
PRIORITIES = ["High", "Medium", "Low"]
LANES = ["call", "reminder", "followup", "task"]

logging.basicConfig(format="%(asctime)s  %(levelname)s  %(message)s", level=logging.INFO)
log = logging.getLogger("capture-bot")

anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
notion = Notion(auth=NOTION_TOKEN)
openai_client = OpenAI(api_key=OPENAI_API_KEY) if (OpenAI and OPENAI_API_KEY) else None
twilio_client = (
    TwilioClient(TWILIO_SID, TWILIO_TOKEN)
    if (TwilioClient and TWILIO_SID and TWILIO_TOKEN and TWILIO_FROM and MY_PHONE)
    else None
)

# Pending call proposals awaiting a tap: token -> {chat_id, when_iso, message}
PROPOSALS: dict[str, dict] = {}
# Chats we've asked "when?" and are waiting on a typed/spoken time: chat_id -> {action, message}
AWAITING_TIME: dict[int, dict] = {}
# Chats that tapped "Note" and owe us note text next: chat_id -> {page_id, task}
AWAITING_NOTE: dict[int, dict] = {}

# --------------------------------------------------------------------------- #
# Tiny persistent store for scheduled reminders (survives restarts)
# --------------------------------------------------------------------------- #
class ReminderStore:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS reminders ("
            "id TEXT PRIMARY KEY, chat_id INTEGER, fire_at TEXT, "
            "kind TEXT, message TEXT, status TEXT DEFAULT 'pending')"
        )
        self.db.commit()

    def add(self, rid, chat_id, fire_at, kind, message):
        self.db.execute(
            "INSERT OR REPLACE INTO reminders VALUES (?,?,?,?,?, 'pending')",
            (rid, chat_id, fire_at, kind, message),
        )
        self.db.commit()

    def mark_done(self, rid):
        self.db.execute("UPDATE reminders SET status='done' WHERE id=?", (rid,))
        self.db.commit()

    def pending(self):
        cur = self.db.execute(
            "SELECT id, chat_id, fire_at, kind, message FROM reminders "
            "WHERE status='pending'"
        )
        return cur.fetchall()


store = ReminderStore(DB_PATH)

# --------------------------------------------------------------------------- #
# Claude parsing / classification
# --------------------------------------------------------------------------- #
SYSTEM_PROMPT = """You turn Brad's quick brain-dump into clean, structured items.
Brad runs a screen-printing company and also manages a lot of personal and family life.

Right now it is <<NOW>> (America/Chicago). Use this to resolve "tomorrow", "Friday",
"in 20 minutes", "at 3:15", etc.

A message may contain several items — split them. Return ONLY a JSON object, no prose,
no markdown fences, in exactly this shape:

{"items": [
  {"task": "...", "category": "...", "priority": "...", "lane": "...",
   "when": "...", "due": "...", "notes": "..."}
]}

Fields:
- "task": short, clear action starting with a verb.
- "category": exactly one of Work, Personal, Katherine. (There is no Church bucket —
  church belongs in Personal.) Use how Brad talks to decide:
    * Personal = his home and life, home projects, music, church, and his family —
                 Hannah, Eliza, Emmie.
    * Katherine = Katherine, her lawyers, Austin Greer, Huskey, buyouts, tasks at
                  Katherine's house, conversations he needs to have with her, and her
                  estate / insurance / finances.
    * Work = Culture Apparel and the team. Anything HR, hiring, or otherwise business
             related. People and their roles:
               - Ben, Cam (a.k.a. Cameron) — outbound sales
               - Abby — marketing / assistant / account management
               - Mia — paid ads and marketing
               - Peter — COO, Brad's #2
               - Vitaliy — operations
               - Gian — production manager
               - Parker, Asher, Jeff, Zach — production
             A task naming any of these people, by itself, is almost always Work
             (unless clearly about his personal life).
- "priority": one of High, Medium, Low. If he says "important" or "urgent", set High.
- "lane": one of call, reminder, followup, task.
    * "call"     = he flagged it "important" / "urgent" / "call me", OR it's a specific-
                   time thing where missing it matters ("call Katherine at 3:15",
                   "leave by 4:30"). This lane lets him pick call vs notification vs plain
                   task. A clock time in "when" is preferred but NOT required.
    * "reminder" = a specific clock time but routine, not urgent ("standup at 10").
                   MUST have a clock time in "when".
    * "followup" = must get done today or by a date, but no clock time and not flagged
                   urgent ("make sure I send the contract today"). Put the date in "due".
    * "task"     = everything else. The default. Most items are this.
- "when": local datetime "YYYY-MM-DDTHH:MM" if a clock time is given, else null.
- "due": date "YYYY-MM-DD" for followup/task deadlines, else null.
- "notes": If he gives extra detail or context beyond the bare action — the why, names,
  amounts, a deadline reason, anything useful — put that context here. If he just states
  a thing with no extra detail, leave null. Keep the "task" itself short; the color goes
  in notes.

When unsure between reminder and task, choose task. Use "call" whenever he signals
urgency/importance ("urgent", "important", "call me", "ASAP") OR a real time-critical
moment — and "urgent"/"important" ALWAYS win over the word "reminder": an "urgent
reminder" is a call, not a reminder."""


def _extract_json(raw: str) -> dict:
    raw = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL)
    if fence:
        raw = fence.group(1).strip()
    s, e = raw.find("{"), raw.rfind("}")
    if s != -1 and e != -1:
        raw = raw[s : e + 1]
    return json.loads(raw)


def parse_items(text: str) -> list[dict]:
    now = datetime.datetime.now(TZ).strftime("%A, %Y-%m-%dT%H:%M")
    resp = anthropic_client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1500,
        system=SYSTEM_PROMPT.replace("<<NOW>>", now),
        messages=[{"role": "user", "content": text}],
    )
    raw = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    items = _extract_json(raw).get("items", [])
    cleaned = []
    for it in items:
        if not it.get("task"):
            continue
        if it.get("category") not in CATEGORIES:
            it["category"] = "Personal"
        if it.get("priority") not in PRIORITIES:
            it["priority"] = "Medium"
        if it.get("lane") not in LANES:
            it["lane"] = "task"
        # A routine "reminder" needs a time; without one it's really a followup.
        if it["lane"] == "reminder" and not it.get("when"):
            it["lane"] = "followup"
        # Urgency words always mean the call lane, even if he wrote "reminder".
        if re.search(r"\b(urgent|important|call me|asap)\b", text, re.I):
            it["lane"] = "call"
        cleaned.append(it)
    return cleaned


# --------------------------------------------------------------------------- #
# Notion write
# --------------------------------------------------------------------------- #
def write_inbox(it: dict, source: str) -> None:
    props = {
        "Task": {"title": [{"text": {"content": it["task"][:200]}}]},
        "Status": {"select": {"name": "Inbox"}},
        "Source": {"select": {"name": source}},
        "Category": {"select": {"name": it["category"]}},
        "Priority": {"select": {"name": it["priority"]}},
    }
    due = it.get("due") or (it.get("when") or "")[:10] or None
    if due:
        props["Due"] = {"date": {"start": due}}
    if it.get("notes"):
        props["Notes"] = {"rich_text": [{"text": {"content": str(it["notes"])[:1900]}}]}
    notion.pages.create(parent={"database_id": NOTION_DB_ID}, properties=props)


def query_open_due_today() -> list[str]:
    today = datetime.datetime.now(TZ).date().isoformat()
    res = notion.databases.query(
        database_id=NOTION_DB_ID,
        filter={
            "and": [
                {"property": "Done", "checkbox": {"equals": False}},
                {"property": "Due", "date": {"on_or_before": today}},
            ]
        },
    )
    out = []
    for row in res.get("results", []):
        title = row["properties"]["Task"]["title"]
        out.append(title[0]["plain_text"] if title else "(untitled)")
    return out


def _plain(rich: list) -> str:
    return "".join(seg.get("plain_text", "") for seg in (rich or []))


def list_open_tasks(limit: int = 12) -> list[dict]:
    """Open (Status=Inbox) Capture Inbox tasks, ordered High -> Low, then by due date."""
    res = notion.databases.query(
        database_id=NOTION_DB_ID,
        filter={"property": "Done", "checkbox": {"equals": False}},
        page_size=100,
    )
    rank = {"High": 0, "Medium": 1, "Low": 2}
    tasks = []
    for row in res.get("results", []):
        p = row["properties"]
        title = _plain(p["Task"]["title"]) or "(untitled)"
        cat = (p.get("Category", {}).get("select") or {}).get("name", "")
        prio = (p.get("Priority", {}).get("select") or {}).get("name", "Medium")
        notes = _plain(p.get("Notes", {}).get("rich_text", []))
        due = (p.get("Due", {}).get("date") or {}).get("start", "")
        tasks.append(
            {"id": row["id"], "task": title, "category": cat,
             "priority": prio, "notes": notes, "due": due}
        )
    tasks.sort(key=lambda t: (rank.get(t["priority"], 1), t["due"] or "9999"))
    return tasks[:limit]


def mark_task_done(page_id: str) -> None:
    notion.pages.update(
        page_id=page_id,
        properties={"Done": {"checkbox": True}, "Status": {"select": {"name": "Done"}}},
    )


def append_task_note(page_id: str, text: str) -> str:
    """Append text to a task's Notes (keeping what's already there). Returns the combined note."""
    page = notion.pages.retrieve(page_id=page_id)
    existing = _plain(page["properties"].get("Notes", {}).get("rich_text", []))
    combined = (existing + "\n" + text).strip() if existing else text.strip()
    combined = combined[:1900]
    notion.pages.update(
        page_id=page_id,
        properties={"Notes": {"rich_text": [{"text": {"content": combined}}]}},
    )
    return combined


# --------------------------------------------------------------------------- #
# Actions: place a call / send a ping
# --------------------------------------------------------------------------- #
def place_call(message: str) -> None:
    vr = VoiceResponse()
    spoken = f"Hi Brad. This is your reminder. {message}. Again. {message}."
    vr.say(spoken, voice="Polly.Matthew")
    vr.pause(length=1)
    twilio_client.calls.create(twiml=str(vr), to=MY_PHONE, from_=TWILIO_FROM)


async def fire_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data
    rid, chat_id, kind, message = (
        data["rid"], data["chat_id"], data["kind"], data["message"],
    )
    try:
        if kind == "call" and twilio_client:
            place_call(message)
            await context.bot.send_message(chat_id, f"📞 Calling you now: {message}")
        else:
            tag = "🚨" if kind == "call" else "⏰"
            await context.bot.send_message(chat_id, f"{tag} {message}")
    except Exception as e:  # noqa: BLE001
        log.exception("fire failed")
        await context.bot.send_message(chat_id, f"⚠️ Reminder fired but errored: {e}")
    finally:
        store.mark_done(rid)


def schedule_reminder(job_queue, chat_id, fire_at, kind, message, rid=None):
    rid = rid or uuid.uuid4().hex[:12]
    store.add(rid, chat_id, fire_at.isoformat(), kind, message)
    job_queue.run_once(
        fire_reminder,
        when=fire_at,
        data={"rid": rid, "chat_id": chat_id, "kind": kind, "message": message},
        name=rid,
    )
    return rid


# --------------------------------------------------------------------------- #
# Telegram handlers
# --------------------------------------------------------------------------- #
def _gate(update: Update) -> str | None:
    chat_id = update.effective_chat.id
    if not AUTHORIZED_CHAT_ID:
        return (
            f"👋 Your chat ID is `{chat_id}`. Set AUTHORIZED_CHAT_ID to it and "
            "restart me so I only listen to you."
        )
    if str(chat_id) != str(AUTHORIZED_CHAT_ID):
        return ""
    return None


def _parse_when(when_iso: str) -> datetime.datetime | None:
    try:
        dt = datetime.datetime.fromisoformat(when_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
        return dt
    except Exception:
        return None


def _fmt_when(dt: datetime.datetime) -> str:
    """Time only if today; otherwise include the date so far-out items are clear."""
    now = datetime.datetime.now(TZ)
    if dt.date() == now.date():
        return f"{dt:%-I:%M %p}"
    if dt.year == now.year:
        return f"{dt:%a %b %-d, %-I:%M %p}"
    return f"{dt:%a %b %-d %Y, %-I:%M %p}"


def _offset_from_code(code: str, now: datetime.datetime) -> datetime.datetime:
    if code == "15m":
        return now + datetime.timedelta(minutes=15)
    if code == "30m":
        return now + datetime.timedelta(minutes=30)
    if code == "1h":
        return now + datetime.timedelta(hours=1)
    if code == "3h":
        return now + datetime.timedelta(hours=3)
    if code == "eve":
        t = now.replace(hour=18, minute=0, second=0, microsecond=0)
        return t if t > now else t + datetime.timedelta(days=1)
    if code == "tom":
        return (now + datetime.timedelta(days=1)).replace(
            hour=9, minute=0, second=0, microsecond=0
        )
    if code == "tom_pm":
        return (now + datetime.timedelta(days=1)).replace(
            hour=13, minute=0, second=0, microsecond=0
        )
    if code == "3d":
        return (now + datetime.timedelta(days=3)).replace(
            hour=9, minute=0, second=0, microsecond=0
        )
    return now + datetime.timedelta(minutes=30)


def _when_keyboard(token: str, action: str) -> InlineKeyboardMarkup:
    def b(label, code):
        return InlineKeyboardButton(label, callback_data=f"when:{token}:{action}:{code}")

    return InlineKeyboardMarkup(
        [
            [b("1 hr", "1h"), b("3 hr", "3h"), b("Tonight", "eve")],
            [b("Tom 9am", "tom"), b("Tom 1pm", "tom_pm"), b("In 3 days", "3d")],
            [b("📅 Pick date & time", "other")],
        ]
    )


def _schedule_choice(job_queue, chat_id, action, fire_at, msg) -> None:
    schedule_reminder(job_queue, chat_id, fire_at, "call" if action == "call" else "ping", msg)


def _choice_confirm(action: str, fire_at: datetime.datetime, msg: str) -> str:
    t = _fmt_when(fire_at)
    if action == "call":
        if twilio_client:
            return f'📞 Call set for {t}: "{msg}"'
        return (
            f'🚨 Set for {t}: "{msg}"\n'
            "(Loud Telegram ping — add your Twilio keys for a real call.)"
        )
    return f'🔔 Notification set for {t}: "{msg}"'


def _parse_time_phrase(text: str) -> datetime.datetime | None:
    """Turn a free-text time ('in 20 min', '3:15pm', 'tomorrow 9am') into a datetime."""
    now = datetime.datetime.now(TZ).strftime("%A, %Y-%m-%dT%H:%M")
    try:
        resp = anthropic_client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=40,
            system=(
                "Convert the user's phrase to one local datetime formatted exactly as "
                f"YYYY-MM-DDTHH:MM, relative to now ({now}, America/Chicago). "
                "Reply with ONLY that datetime, or the word null if there is no time."
            ),
            messages=[{"role": "user", "content": text}],
        )
        raw = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        m = re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", raw)
        return _parse_when(m.group(0)) if m else None
    except Exception:
        return None


async def _handle_items(update, context, text, source):
    if not text or not text.strip():
        await update.message.reply_text("⚠️ Empty message — nothing saved.")
        return

    # If we asked "when?" for an urgent item, treat this message as the answer.
    chat_id = update.effective_chat.id

    # If they tapped "Note" on a task, this message is the note text.
    note_pend = AWAITING_NOTE.pop(chat_id, None)
    if note_pend:
        try:
            append_task_note(note_pend["page_id"], text.strip())
            await update.message.reply_text(f'📝 Note added to "{note_pend["task"]}".')
        except Exception as e:  # noqa: BLE001
            log.exception("note append failed")
            await update.message.reply_text(f"⚠️ Couldn't save that note: {e}")
        return

    pend = AWAITING_TIME.pop(chat_id, None)
    if pend:
        fire_at = _parse_time_phrase(text)
        if fire_at:
            now = datetime.datetime.now(TZ)
            if fire_at <= now:
                fire_at = now + datetime.timedelta(seconds=10)
            _schedule_choice(context.job_queue, chat_id, pend["action"], fire_at, pend["message"])
            await update.message.reply_text(
                _choice_confirm(pend["action"], fire_at, pend["message"])
            )
            return
        await update.message.reply_text(
            f'(Couldn\'t catch a time — left "{pend["message"]}" as a filed task. '
            "Treating your message as something new:)"
        )
        # fall through and process this text as a fresh capture

    try:
        items = parse_items(text)
    except Exception as e:  # noqa: BLE001
        log.exception("parse failed")
        await update.message.reply_text(f"⚠️ Couldn't read that — nothing saved. ({e})")
        return
    if not items:
        await update.message.reply_text("🤔 No task found in that — nothing saved.")
        return

    chat_id = update.effective_chat.id
    now = datetime.datetime.now(TZ)
    filed_lines = []

    for it in items:
        # Always keep a record in the inbox.
        try:
            write_inbox(it, source)
        except Exception as e:  # noqa: BLE001
            log.exception("notion write failed")
            await update.message.reply_text(f'⚠️ Failed to file "{it["task"]}": {e}')
            continue

        lane = it["lane"]
        if lane == "call":
            when = _parse_when(it.get("when") or "")
            token = uuid.uuid4().hex[:8]
            PROPOSALS[token] = {
                "chat_id": chat_id,
                "when_iso": when.isoformat() if when else None,
                "message": it["task"],
            }
            kb = InlineKeyboardMarkup(
                [[
                    InlineKeyboardButton("📞 Call", callback_data=f"call:{token}"),
                    InlineKeyboardButton("🔔 Notify", callback_data=f"notify:{token}"),
                    InlineKeyboardButton("📝 Just a task", callback_data=f"task:{token}"),
                ]]
            )
            when_label = f" — {_fmt_when(when)}" if when else ""
            await update.message.reply_text(
                f'🚨 *{it["task"]}*{when_label}\nHow do you want this — call, notify, or just filed?',
                reply_markup=kb,
                parse_mode="Markdown",
            )
        elif lane == "reminder":
            when = _parse_when(it.get("when", ""))
            if when and when > now:
                schedule_reminder(context.job_queue, chat_id, when, "ping", it["task"])
                filed_lines.append(f'⏰ {_fmt_when(when)} · "{it["task"]}"')
            else:
                filed_lines.append(f'✅ "{it["task"]}" (time passed — filed)')
        elif lane == "followup":
            due = it.get("due") or now.date().isoformat()
            filed_lines.append(f'📌 by {due} · "{it["task"]}" (I\'ll nudge you)')
        else:  # task
            filed_lines.append(f'✅ {it["category"]} · {it["priority"]} · "{it["task"]}"')

    if filed_lines:
        await update.message.reply_text("\n".join(filed_lines))


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    now = datetime.datetime.now(TZ)
    data = q.data

    # Task list actions (from /today): complete or add a note.
    if data.startswith("done:"):
        page_id = data[5:]
        label = (q.message.text or "task").splitlines()[0] if q.message and q.message.text else "task"
        try:
            mark_task_done(page_id)
            await q.edit_message_text(f"✅ Done — {label}")
        except Exception as e:  # noqa: BLE001
            log.exception("mark done failed")
            await q.edit_message_text(f"⚠️ Couldn't mark it done: {e}")
        return

    if data.startswith("note:"):
        page_id = data[5:]
        chat_id = update.effective_chat.id
        label = (q.message.text or "this task").splitlines()[0] if q.message and q.message.text else "this task"
        AWAITING_NOTE[chat_id] = {"page_id": page_id, "task": label}
        await context.bot.send_message(
            chat_id, "📝 Send the note (text or voice) and I'll add it to that task."
        )
        return

    # Second step: a specific "when" was chosen for an urgent item.
    if data.startswith("when:"):
        _, token, action, code = data.split(":")
        p = PROPOSALS.pop(token, None)
        if not p:
            await q.edit_message_text("This one expired — just send it again.")
            return
        if code == "other":
            AWAITING_TIME[p["chat_id"]] = {"action": action, "message": p["message"]}
            await q.edit_message_text(
                f'📅 When for "{p["message"]}"? Reply with a date and time — '
                '"July 13 at 1pm", "Friday 3:15pm", "in 3 weeks at noon".'
            )
            return
        fire_at = _offset_from_code(code, now)
        _schedule_choice(context.job_queue, p["chat_id"], action, fire_at, p["message"])
        await q.edit_message_text(_choice_confirm(action, fire_at, p["message"]))
        return

    # First step: call / notify / just-a-task.
    action, _, token = data.partition(":")
    p = PROPOSALS.get(token)
    if not p:
        await q.edit_message_text("This one expired — just send it again.")
        return
    msg = p["message"]
    when = _parse_when(p["when_iso"]) if p.get("when_iso") else None

    if action == "task":
        PROPOSALS.pop(token, None)
        await q.edit_message_text(f'📝 Filed as a task: "{msg}"')
        return

    # For a CALL, let Brad pick the date/time himself — never auto-suggest one.
    if action == "call":
        await q.edit_message_text(
            f'📞 When should I call you about "{msg}"?',
            reply_markup=_when_keyboard(token, "call"),
        )
        return

    # Notify: same tap-to-pick date/time as calls, for consistency — no auto time.
    await q.edit_message_text(
        f'🔔 When should I ping you about "{msg}"?',
        reply_markup=_when_keyboard(token, "notify"),
    )
    return


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    gate = _gate(update)
    if gate is not None:
        if gate:
            await update.message.reply_text(gate, parse_mode="Markdown")
        return
    await _handle_items(update, context, update.message.text, "Text")


async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    gate = _gate(update)
    if gate is not None:
        if gate:
            await update.message.reply_text(gate, parse_mode="Markdown")
        return
    if not openai_client:
        await update.message.reply_text("🎙️ Add OPENAI_API_KEY to enable voice.")
        return
    await update.effective_chat.send_action("typing")
    voice = update.message.voice or update.message.audio
    path = tempfile.NamedTemporaryFile(suffix=".ogg", delete=False).name
    try:
        await (await voice.get_file()).download_to_drive(path)
        with open(path, "rb") as af:
            text = openai_client.audio.transcriptions.create(
                model="whisper-1", file=af
            ).text
    except Exception as e:  # noqa: BLE001
        log.exception("transcription failed")
        await update.message.reply_text(f"⚠️ Couldn't transcribe — nothing saved. ({e})")
        return
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    await _handle_items(update, context, text, "Voice")


async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not AUTHORIZED_CHAT_ID:
        await update.message.reply_text(
            f"👋 Capture bot here. Your chat ID is `{chat_id}` — set "
            "AUTHORIZED_CHAT_ID to it and restart me to lock me to you.",
            parse_mode="Markdown",
        )
        return
    await update.message.reply_text(
        "👋 Ready. Talk or type — I'll sort each thing into Work, Personal, or Katherine:\n"
        "• say *urgent / important* (or give a time) → I ask: call, notify, or just file it\n"
        "• must-do today → I file it and nudge you\n"
        "• everything else → straight to your inbox\n"
        "Add detail and I'll tuck it into the notes.",
        parse_mode="Markdown",
    )


_PRIO_EMOJI = {"High": "🔴", "Medium": "🟡", "Low": "⚪"}


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    gate = _gate(update)
    if gate is not None:
        if gate:
            await update.message.reply_text(gate, parse_mode="Markdown")
        return
    try:
        tasks = list_open_tasks(limit=12)
    except Exception as e:  # noqa: BLE001
        log.exception("list tasks failed")
        await update.message.reply_text(f"⚠️ Couldn't load your tasks: {e}")
        return
    if not tasks:
        await update.message.reply_text("🎉 Nothing open — your inbox is clear.")
        return

    await update.message.reply_text(f"🗂️ Your open tasks ({len(tasks)}) — tap to close out:")
    for t in tasks:
        emoji = _PRIO_EMOJI.get(t["priority"], "🟡")
        cat = f"{t['category']} — " if t["category"] else ""
        line = f"{emoji} {cat}{t['task']}"
        if t["due"]:
            line += f"\n⏳ due {t['due']}"
        if t["notes"]:
            line += f"\n📝 {t['notes']}"
        kb = InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("✓ Done", callback_data=f"done:{t['id']}"),
                InlineKeyboardButton("📝 Note", callback_data=f"note:{t['id']}"),
            ]]
        )
        await update.message.reply_text(line, reply_markup=kb)


# --------------------------------------------------------------------------- #
# Startup + nudges
# --------------------------------------------------------------------------- #
async def nudge(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not AUTHORIZED_CHAT_ID:
        return
    try:
        items = query_open_due_today()
    except Exception:
        log.exception("nudge query failed")
        return
    if items:
        lines = "\n".join(f"• {t}" for t in items[:20])
        await context.bot.send_message(
            int(AUTHORIZED_CHAT_ID), f"📋 Still open and due:\n{lines}"
        )


async def on_startup(app: Application) -> None:
    # Re-arm any reminders that were pending when we last stopped.
    now = datetime.datetime.now(TZ)
    for rid, chat_id, fire_at, kind, message in store.pending():
        when = _parse_when(fire_at)
        if not when:
            store.mark_done(rid)
            continue
        if when <= now - datetime.timedelta(minutes=30):
            store.mark_done(rid)  # too stale to fire
            try:
                await app.bot.send_message(chat_id, f"⚠️ Missed while offline: {message}")
            except Exception:
                pass
            continue
        fire_at_dt = max(when, now + datetime.timedelta(seconds=10))
        app.job_queue.run_once(
            fire_reminder,
            when=fire_at_dt,
            data={"rid": rid, "chat_id": chat_id, "kind": kind, "message": message},
            name=rid,
        )
    # Schedule the daily escalation nudges.
    for t in NUDGE_TIMES.split(","):
        try:
            hh, mm = (int(x) for x in t.strip().split(":"))
            app.job_queue.run_daily(
                nudge, time=datetime.time(hh, mm, tzinfo=TZ), name=f"nudge-{t.strip()}"
            )
        except Exception:
            log.warning("bad NUDGE_TIMES entry: %r", t)
    log.info("Startup complete; reminders re-armed and nudges scheduled.")


def main() -> None:
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(on_startup)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("tasks", cmd_today))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("Capture bot v2 running (long polling). Ctrl-C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
