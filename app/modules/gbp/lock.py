"""Profile lock (B5): save an approved snapshot of the public listing, re-check it every 6 hours,
and flag any field Google or a user suggestion changed. In connected mode you can revert a change
through the Business Profile API; otherwise the app tells you exactly what to fix."""
from datetime import datetime, timedelta, timezone

from app import db, jobs, places_store, settings
from app.providers import gbp_api, places

FIELDS = {"name": "Business name", "phone": "Phone number", "website": "Website", "address": "Address",
          "primary_category": "Primary category", "hours": "Opening hours", "business_status": "Open / closed status"}
INTERVAL = timedelta(hours=6)


def live_snapshot(company: dict) -> dict:
    place = places_store.get(company["place_id"]) if company["place_id"] else None
    if not place or not place.get("google_place_id") or place["google_place_id"].startswith("demo-"):
        raise ValueError("This client has no Google place to monitor.")
    fresh = places.details(place["google_place_id"])
    places_store.upsert(fresh)
    return {k: fresh.get(k) for k in FIELDS}


def approve(company_id: int, data: dict | None = None) -> None:
    co = db.one("SELECT * FROM companies WHERE id = ?", company_id)
    data = data or live_snapshot(co)
    db.run("UPDATE profile_snapshots SET approved = 0 WHERE company_id = ?", company_id)
    db.insert("profile_snapshots", company_id=company_id, data=data, approved=1, created_at=db.now())
    db.run("UPDATE profile_changes SET status = 'accepted', resolved_at = ? WHERE company_id = ? AND status = 'open'",
           db.now(), company_id)
    db.update("companies", company_id, lock_checked_at=db.now())


def approved_snapshot(company_id: int) -> dict | None:
    row = db.one("SELECT data FROM profile_snapshots WHERE company_id = ? AND approved = 1 ORDER BY id DESC LIMIT 1",
                 company_id)
    return db.loads(row["data"], None) if row else None


def diff(approved: dict, live: dict) -> list[tuple[str, object, object]]:
    def norm(v):
        return v if not isinstance(v, str) else " ".join(v.split()).strip().lower()
    return [(k, approved.get(k), live.get(k)) for k in FIELDS if norm(approved.get(k)) != norm(live.get(k))]


def check(company_id: int, live: dict | None = None) -> int:
    """Compare live vs approved; record new changes. Returns number of open changes."""
    co = db.one("SELECT * FROM companies WHERE id = ?", company_id)
    approved = approved_snapshot(company_id)
    if not approved:
        return 0
    live = live or live_snapshot(co)
    for field, old, new in diff(approved, live):
        exists = db.one("SELECT 1 FROM profile_changes WHERE company_id = ? AND field = ? AND status = 'open'",
                        company_id, field)
        if not exists:
            db.insert("profile_changes", company_id=company_id, field=field, approved_value=_s(old),
                      live_value=_s(new), status="open", detected_at=db.now())
    db.update("companies", company_id, lock_checked_at=db.now())
    return db.one("SELECT COUNT(*) n FROM profile_changes WHERE company_id = ? AND status = 'open'", company_id)["n"]


def _s(v) -> str:
    return "\n".join(v) if isinstance(v, list) else ("" if v is None else str(v))


REVERTIBLE = {"name": ("title", lambda v: v), "website": ("websiteUri", lambda v: v),
              "phone": ("phoneNumbers", lambda v: {"primaryPhone": v})}


def revert(change_id: int) -> str:
    ch = db.one("SELECT * FROM profile_changes WHERE id = ?", change_id)
    co = db.one("SELECT * FROM companies WHERE id = ?", ch["company_id"])
    if gbp_api.connected() and co["gbp_location"] and ch["field"] in REVERTIBLE:
        api_field, wrap = REVERTIBLE[ch["field"]]
        gbp_api.update_location(co["gbp_location"], {api_field: wrap(ch["approved_value"])}, api_field)
        db.update("profile_changes", change_id, status="reverted", resolved_at=db.now())
        return "reverted"
    return "manual"  # stays open until you fix it in Google and mark it fixed


@jobs.on_tick
def poll() -> None:
    if not settings.enabled("places"):
        return
    cutoff = (datetime.now(timezone.utc) - INTERVAL).isoformat(timespec="seconds")
    for co in db.all("SELECT c.id FROM companies c WHERE c.is_client = 1 AND (c.lock_checked_at IS NULL OR "
                     "c.lock_checked_at < ?) AND EXISTS (SELECT 1 FROM profile_snapshots s WHERE s.company_id = c.id "
                     "AND s.approved = 1)", cutoff):
        try:
            check(co["id"])
        except Exception:
            db.update("companies", co["id"], lock_checked_at=db.now())
