"""Outbox: every outgoing email/SMS is a row here.

- Messages to cold contacts (found by prospecting or imported) start as `draft` and need
  your approval. Replies to people who contacted you (forms, review requests) may be
  created as `approved`.
- Approved emails are sent by SMTP when configured. SMS has no gateway in this build:
  approved SMS stay in the outbox with a copy button so you send them from your phone.
"""
import smtplib
from email.message import EmailMessage

from app import db, jobs, settings
from app.web import brand

COLD_SOURCES = {"prospecting", "import", "manual"}


def footer() -> str:
    b = brand()
    lines = ["", "--", b["name"]]
    if b["address"]:
        lines.append(b["address"])
    lines.append("Not interested? Reply \"unsubscribe\" and we won't contact you again.")
    return "\n".join(lines)


def queue(channel: str, contact_id: int | None, to_addr: str, subject: str, body: str, *,
          origin: str, approved: bool = False) -> int | None:
    """Add a message. Returns the outbox id, or None if the contact must not be contacted."""
    contact = db.one("SELECT * FROM contacts WHERE id = ?", contact_id) if contact_id else None
    if contact and contact["do_not_contact"]:
        return None
    if contact and contact["source"] in COLD_SOURCES:
        approved = False  # cold outreach is never sent without you approving it
    if channel == "email" and not body.rstrip().endswith(footer().strip()):
        body = body.rstrip() + "\n" + footer()
    return db.insert("outbox", channel=channel, contact_id=contact_id, to_addr=to_addr, subject=subject,
                     body=body, status="approved" if approved else "draft", origin=origin,
                     created_at=db.now())


def send(outbox_id: int) -> None:
    m = db.one("SELECT * FROM outbox WHERE id = ?", outbox_id)
    if not m or m["status"] != "approved" or m["channel"] != "email":
        return
    if not settings.enabled("smtp"):
        return  # stays approved until SMTP is configured
    msg = EmailMessage()
    msg["From"] = settings.env("SMTP_FROM") or settings.env("SMTP_USER")
    msg["To"] = m["to_addr"]
    msg["Subject"] = m["subject"] or ""
    if brand()["email"]:
        msg["Reply-To"] = brand()["email"]
    msg.set_content(m["body"] or "")
    try:
        with smtplib.SMTP(settings.env("SMTP_HOST"), int(settings.env("SMTP_PORT", "587")), timeout=30) as s:
            s.starttls()
            if settings.env("SMTP_USER"):
                s.login(settings.env("SMTP_USER"), settings.env("SMTP_PASSWORD"))
            s.send_message(msg)
    except Exception as exc:
        db.update("outbox", outbox_id, status="failed", error=str(exc)[:300])
        return
    db.update("outbox", outbox_id, status="sent", sent_at=db.now())
    if m["contact_id"]:
        db.insert("activities", contact_id=m["contact_id"], type="email",
                  body=f"Sent: {m['subject']}", created_at=db.now())


@jobs.on_tick
def send_approved() -> None:
    for m in db.all("SELECT id FROM outbox WHERE status = 'approved' AND channel = 'email' LIMIT 20"):
        send(m["id"])
