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

from __future__ import annotations

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

CATEGORIES = ["Work", "Personal", "Katherine", "Church"]
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

# --------------------------------------------------------------------------- #
# Tiny persistent store for scheduled reminders (survives restarts)
# --------------------------------------------------------------------------- #
class ReminderStore:
    def __init__(self, path: str):
        # Make sure the DB's parent dir exists (e.g. a Railway volume at /data).
        # If the volume isn't mounted, fall back to creating the dir in container
        # storage so the bot still boots instead of crash-looping on startup.
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
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
- "category": one of Work, Personal, Katherine, Church.
    Katherine = anything about Katherine, Austin's estate, her insurance/legal/finances,
    or supporting her.
- "priority": one of High, Medium, Low.
- "lane": one of call, reminder, followup, task. Choose carefully:
    * "call"     = time-critical AND must interrupt him. There is a specific clock
                   time and missing it matters — e.g. "call Katherine at 3:15",
                   "leave by 4:30 for the airport", or any specific-time item flagged
                   urgent / "make sure" / "don't let me miss" / "call me". MUST have a
                   clock time in "when".
    * "reminder" = tied to a specific clock time but routine, not critical
                   ("standup at 10", "take meds at 9"). MUST have a clock time in "when".
    * "followup" = must get done today or by a date, but NO specific clock time
                   ("make sure I send the contract today", "don't forget to call the
                   bank this week"). Put the deadline in "due".
    * "task"     = everything else. The default. Most items are this.
- "when": local datetime "YYYY-MM-DDTHH:MM" for call/reminder lanes, else null.
- "due": date "YYYY-MM-DD" for followup/task deadlines, else null.
- "notes": extra context/names/links, else null.

Be conservative with "call" — only when a miss genuinely costs him. When unsure between
call and reminder, choose reminder. When unsure between followup and task, choose task."""


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
        # A "call" with no time can't be a call — drop it to a followup.
        if it["lane"] in ("call", "reminder") and not it.get("when"):
            it["lane"] = "followup"
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
                {"property": "Status", "select": {"equals": "Inbox"}},
                {"property": "Due", "date": {"on_or_before": today}},
            ]
        },
    )
    out = []
    for row in res.get("results", []):
        title = row["properties"]["Task"]["title"]
        out.append(title[0]["plain_text"] if title else "(untitled)")
    return out


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


async def _handle_items(update, context, text, source):
    if not text or not text.strip():
        await update.message.reply_text("⚠️ Empty message — nothing saved.")
        return
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
            when = _parse_when(it.get("when", ""))
            if not when:
                filed_lines.append(f'✅ {it["category"]} · {it["priority"]} · "{it["task"]}"')
                continue
            token = uuid.uuid4().hex[:8]
            PROPOSALS[token] = {
                "chat_id": chat_id,
                "when_iso": when.isoformat(),
                "message": it["task"],
            }
            kb = InlineKeyboardMarkup(
                [[
                    InlineKeyboardButton("📞 Call me", callback_data=f"call:{token}"),
                    InlineKeyboardButton("⏰ Just ping", callback_data=f"ping:{token}"),
                    InlineKeyboardButton("🗑️", callback_data=f"cancel:{token}"),
                ]]
            )
            await update.message.reply_text(
                f'🚨 *{it["task"]}* at {when:%-I:%M %p}.\n'
                f"I'll call you ~{CALL_LEAD_MINUTES} min before. Confirm?",
                reply_markup=kb,
                parse_mode="Markdown",
            )
        elif lane == "reminder":
            when = _parse_when(it.get("when", ""))
            if when and when > now:
                schedule_reminder(context.job_queue, chat_id, when, "ping", it["task"])
                filed_lines.append(f'⏰ {when:%-I:%M %p} · "{it["task"]}"')
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
    action, _, token = q.data.partition(":")
    p = PROPOSALS.pop(token, None)
    if not p:
        await q.edit_message_text("This one expired — just send it again.")
        return
    when = _parse_when(p["when_iso"])
    msg = p["message"]
    now = datetime.datetime.now(TZ)

    if action == "cancel":
        await q.edit_message_text(f'🗑️ Cancelled: "{msg}"')
        return

    if action == "call":
        fire_at = when - datetime.timedelta(minutes=CALL_LEAD_MINUTES)
        if fire_at <= now:
            fire_at = now + datetime.timedelta(seconds=10)
        schedule_reminder(context.job_queue, p["chat_id"], fire_at, "call", msg)
        if twilio_client:
            await q.edit_message_text(f'📞 Set — I\'ll call you ~{when:%-I:%M %p}: "{msg}"')
        else:
            await q.edit_message_text(
                f'🚨 Set for {when:%-I:%M %p}: "{msg}"\n'
                "(Calls aren't configured yet — you'll get a loud Telegram ping. "
                "Add Twilio keys to enable real calls.)"
            )
    else:  # ping
        fire_at = when if when > now else now + datetime.timedelta(seconds=10)
        schedule_reminder(context.job_queue, p["chat_id"], fire_at, "ping", msg)
        await q.edit_message_text(f'⏰ Ping set for {when:%-I:%M %p}: "{msg}"')


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
        "👋 Ready. Talk or type — I'll sort each thing into the right lane:\n"
        "• urgent w/ a time → I propose a call (one tap to confirm)\n"
        "• a time, routine → I ping you then\n"
        "• must-do today → I file it and nudge you\n"
        "• everything else → straight to your inbox"
    )


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
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("Capture bot v2 running (long polling). Ctrl-C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
