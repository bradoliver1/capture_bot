# 📥 Capture Bot v2

One Telegram brain-dump in. Claude reads each thing you say and routes it by urgency:

| Lane | When it's chosen | What happens |
|---|---|---|
| **call** | time-critical, must interrupt you ("call Katherine at 3:15", "urgent", "make sure I…") | Bot proposes a phone call → **one tap to confirm** → it calls you ~5 min before, plus a ping |
| **reminder** | a clock time but routine ("standup at 10") | Auto Telegram ping at that time |
| **followup** | must happen today / by a date, no set time ("make sure I send the contract today") | Filed with a due date; re-surfaced through the day until done |
| **task** | everything else (the default) | Straight into your Capture Inbox |

Your inbox: **https://www.notion.so/0dc1947f53ce43b9bb0de5de1852aa00**

Everything is also filed to the inbox for the record. The classifier is deliberately
**conservative about "call"** — and even then it never calls without your one tap, so a
misread can't ring you for nothing, and you can catch a miss before it matters.

---

## 🏁 Start here — how to actually run this

You do the **account + key** parts (nobody can log into your accounts but you). **Claude
Code does the technical part** — deploying, running, and fixing first-run errors. Keys go
in your own `.env` file, never into a chat.

### Part 1 — You, by hand (~15 min): collect the keys
Work down the **One-time setup** list below and copy each value into a copy of
`.env.example` saved as `.env`:
1. Telegram bot token (@BotFather)
2. Anthropic key
3. OpenAI key
4. Notion integration token **+ share the Capture Inbox with it**
5. Twilio: Account SID, Auth Token, a number, your cell

(Leave `AUTHORIZED_CHAT_ID` blank for now — you'll fill it in at the very end.)

### Part 2 — Hand it to Claude Code
Open this folder in Claude Code and paste this:

> Set up and run this Telegram capture bot. I've filled in `.env` with my keys. Install
> the dependencies, get it running somewhere always-on (Railway, or just on this machine
> for now), start it, and help me fix any errors. Then tell me how to send my first test.

Claude Code will install everything, deploy it, start it, and troubleshoot the hiccups.

**Note:** it has to live somewhere always-on — Railway (~$5/mo) or a machine you keep on.
Claude Code sets up whichever you choose; it just can't host it for free (this is a
standalone service, not a Claude Code routine).

### Part 3 — Lock it to you (30 sec)
Message your bot once → it replies your chat ID → paste that into `AUTHORIZED_CHAT_ID` →
have Claude Code restart it. Done. Now talk or type and it sorts everything for you.

---

## One-time setup (detail)

### Core
1. **Telegram** — @BotFather → `/newbot` → token → `TELEGRAM_TOKEN`.
2. **Anthropic key** — console.anthropic.com → `ANTHROPIC_API_KEY`.
3. **OpenAI key** — platform.openai.com/api-keys → `OPENAI_API_KEY` (voice).
4. **Notion** — notion.so/profile/integrations → New integration → `NOTION_TOKEN`,
   **then** open the Capture Inbox → ••• → Connections → add the integration. (Without
   this, writes fail.)

### Call tier (Twilio) — the part that breaks through a meeting
5. Make a **Twilio** account → console.twilio.com.
6. Copy your **Account SID** and **Auth Token** from the dashboard.
7. Buy a phone number (~$1/mo) → `TWILIO_FROM_NUMBER`.
8. Set `MY_PHONE_NUMBER` to your cell, E.164 format (e.g. `+16155559876`).
   - On a trial account Twilio can only call *verified* numbers — verify your cell, or
     upgrade (a few dollars) to call freely.
   - **iPhone tip:** so a call rings through Do Not Disturb, add the Twilio number to a
     contact and allow it in your Focus, or enable "repeated calls."

---

## How it feels to use
- *"Call Katherine at 3:15"* → 🚨 proposal + buttons → tap **📞 Call me** → it calls ~3:10.
- *"Order navy blanks for the August run"* → ✅ Work · Medium, into the inbox.
- *"Make sure I send Ramos the signed agreement today"* → 📌 due today, nudged at 1pm/4:30 if still open.
- Dump several at once; it splits and routes each.

Reminders are saved to `reminders.db`, so a restart or redeploy won't drop a scheduled
call. If the bot is offline at the exact fire time, it tells you it missed it rather than
failing silently.

## What's next
- **Calendar visibility:** also drop timed items onto your Google Calendar (small
  fast-follow — needs a one-time Google auth step).
- **Promote-to-team:** push *Work* items from the inbox into your ✅ Tasks DB with the
  right Owner/Access.

## Notes
- Costs are small: Haiku parsing is fractions of a cent; Whisper ~half a cent/voice note;
  Twilio calls a penny or two each.
- Keep `.env` private — it's gitignored.
