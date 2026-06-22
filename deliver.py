"""
Delivery helpers for the morning brief routine.
Telegram + SendGrid HTTPS API. No SMTP needed.
All secrets from environment variables.
"""

import json, os, urllib.request, urllib.error

def send_telegram(text):
    token = os.environ.get("TELEGRAM_TOKEN", "")
    chat_id = os.environ.get("AUTHORIZED_CHAT_ID", "")
    if not token or not chat_id:
        return False, "TELEGRAM_TOKEN or AUTHORIZED_CHAT_ID not set"
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            r = json.loads(resp.read())
            return (True, "") if r.get("ok") else (False, str(r))
    except Exception as e:
        return False, str(e)

def send_email(subject, body):
    api_key = os.environ.get("SENDGRID_API_KEY", "")
    addr = os.environ.get("GMAIL_ADDRESS", "")
    if not api_key or not addr:
        return False, "SENDGRID_API_KEY or GMAIL_ADDRESS not set"
    payload = json.dumps({
        "personalizations": [{"to": [{"email": addr}]}],
        "from": {"email": addr},
        "subject": subject,
        "content": [{"type": "text/plain", "value": body}],
    }).encode()
    req = urllib.request.Request(
        "https://api.sendgrid.com/v3/mail/send",
        data=payload,
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return True, f"HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.read().decode()}"
    except Exception as e:
        return False, str(e)

def deliver_brief(weekday_date, brief_text):
    tg_ok, tg_err = send_telegram(brief_text)
    print(f"Telegram: {'OK' if tg_ok else 'FAILED — ' + tg_err}")
    em_ok, em_err = send_email(f"☀️ Morning Brief — {weekday_date}", brief_text)
    print(f"Email:    {'OK' if em_ok else 'FAILED — ' + em_err}")
    if not tg_ok and not em_ok:
        raise RuntimeError(f"Both channels failed. TG: {tg_err} | Email: {em_err}")
