"""Notifications: a notice bar on every page, automatic review sync, and an optional daily email digest.

Notices are computed from the database (no separate store). "Seen" is a timestamp you set by
dismissing the bar; profile changes, due tasks and drafts stay until you deal with them.
"""
from datetime import datetime, timedelta, timezone

from app import db, jobs, journal, settings

SYNC_EVERY = timedelta(hours=6)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def notices() -> list[dict]:
    seen = db.get_setting("notices_seen", "1970-01-01T00:00:00+00:00")
    now = datetime.now(timezone.utc)
    out = []
    new_reviews = db.all("SELECT r.company_id, c.name, COUNT(*) n FROM reviews r JOIN companies c ON c.id = r.company_id "
                         "WHERE r.created_at > ? GROUP BY r.company_id", seen)
    for r in new_reviews:
        out.append({"level": "info", "text": f"{r['n']} new review{'s' if r['n'] > 1 else ''} for {r['name']}",
                    "href": f"/clients/{r['company_id']}/reviews"})
    changes = db.all("SELECT x.company_id, c.name, COUNT(*) n FROM profile_changes x JOIN companies c ON c.id = x.company_id "
                     "WHERE x.status = 'open' GROUP BY x.company_id")
    for c in changes:
        out.append({"level": "bad", "text": f"{c['n']} unapproved change{'s' if c['n'] > 1 else ''} on {c['name']}'s Google listing",
                    "href": f"/clients/{c['company_id']}/lock"})
    due = db.one("SELECT COUNT(*) n FROM tasks WHERE done = 0 AND due_at IS NOT NULL AND due_at <= ?", _iso(now))["n"]
    if due:
        out.append({"level": "warn", "text": f"{due} task{'s' if due > 1 else ''} due", "href": "/crm/tasks"})
    drafts = db.one("SELECT COUNT(*) n FROM outbox WHERE status = 'draft'")["n"]
    if drafts:
        out.append({"level": "info", "text": f"{drafts} message{'s' if drafts > 1 else ''} waiting for your approval",
                    "href": "/crm/outbox"})
    fails = db.one("SELECT COUNT(*) n FROM events WHERE kind = 'failure' AND created_at > ?", seen)["n"]
    if fails:
        out.append({"level": "bad", "text": f"{fails} failure{'s' if fails > 1 else ''} recorded", "href": "/journal?kind=failure"})
    proposed = db.one("SELECT COUNT(*) n FROM lessons WHERE status = 'proposed'")["n"]
    if proposed:
        out.append({"level": "info", "text": f"{proposed} lesson{'s' if proposed > 1 else ''} to review", "href": "/journal"})
    return out


def mark_seen() -> None:
    db.set_setting("notices_seen", _iso(datetime.now(timezone.utc)))


@jobs.on_tick
def auto_sync_reviews() -> None:
    from app.modules.gbp import reviews
    from app.providers import gbp_api
    if not gbp_api.connected():
        return
    cutoff = _iso(datetime.now(timezone.utc) - SYNC_EVERY)
    for co in db.all("SELECT id FROM companies WHERE is_client = 1 AND gbp_location IS NOT NULL"):
        key = f"reviews_synced_{co['id']}"
        if db.get_setting(key, "") > cutoff:
            continue
        db.set_setting(key, _iso(datetime.now(timezone.utc)))
        try:
            n = reviews.sync_from_google(co["id"])
            if n:
                journal.record("reviews", "outcome", "new_reviews", company_id=co["id"], detail=f"{n} new reviews synced")
        except Exception as exc:
            journal.failure("reviews", "auto_sync", exc, company_id=co["id"])


@jobs.on_tick
def daily_digest() -> None:
    """Once a day, email yourself the notices (needs SMTP and a reply-to email in Settings → Branding)."""
    from app import mailer
    from app.web import brand
    to = brand()["email"]
    if not (settings.enabled("smtp") and to):
        return
    today = datetime.now().date().isoformat()
    if db.get_setting("digest_sent") == today:
        return
    items = notices()
    db.set_setting("digest_sent", today)
    if not items:
        return
    body = "Today in MapProspector:\n\n" + "\n".join(f"- {n['text']}: {settings.PUBLIC_BASE_URL}{n['href']}" for n in items)
    mailer.queue("email", None, to, f"MapProspector digest {today}", body, origin="digest", approved=True)
    db.run("UPDATE tasks SET notified = 1 WHERE done = 0 AND due_at <= ?", _iso(datetime.now(timezone.utc)))
