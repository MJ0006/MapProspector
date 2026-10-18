"""Citation audit (C1) and citation profile/tracker (C2).

Per Local SEO Brain (wiki/backlinks/backlinking-local-citations.md): listings are compared with
owner-confirmed business details, each record keeps directory, URL, observed status and
uncertainty, and "not found" means the check could not verify a listing, not that none exists.

Statuses: live (found, consistent) · wrong (found, some field differs) · missing (not found)
          pending (not checked yet) · unavailable (check failed)
Score = (live + 0.5 × wrong) ÷ (live + wrong + missing) × 100, then a letter grade.
"""
import re

from app import ai, db, jobs, places_store
from app.modules.aivis.engine import domain

DIRECTORIES = [
    # (name, site, vertical) — vertical None = every business
    ("Google Business Profile", "google.com/maps", None), ("Apple Maps", "maps.apple.com", None),
    ("Bing Places", "bing.com/maps", None), ("Yelp", "yelp.com", None), ("Facebook", "facebook.com", None),
    ("Better Business Bureau", "bbb.org", None), ("Yellow Pages", "yellowpages.com", None),
    ("Nextdoor", "nextdoor.com", None), ("Foursquare", "foursquare.com", None), ("MapQuest", "mapquest.com", None),
    ("Superpages", "superpages.com", None), ("Manta", "manta.com", None), ("Hotfrog", "hotfrog.com", None),
    ("Cylex", "cylex.us.com", None), ("EZlocal", "ezlocal.com", None), ("Chamber of Commerce", "chamberofcommerce.com", None),
    ("Angi", "angi.com", "home"), ("Thumbtack", "thumbtack.com", "home"), ("Houzz", "houzz.com", "home"),
    ("Tripadvisor", "tripadvisor.com", "hospitality"), ("OpenTable", "opentable.com", "hospitality"),
    ("Healthgrades", "healthgrades.com", "health"), ("Zocdoc", "zocdoc.com", "health"),
    ("Avvo", "avvo.com", "legal"), ("Justia", "justia.com", "legal"),
]
VERTICALS = {"home": "Home services", "hospitality": "Restaurants & hotels", "health": "Health", "legal": "Legal"}
FIELDS = ["name", "address", "phone", "hours", "website", "categories", "photos", "description"]
PROFILE_KEYS = ["name", "street", "city", "state", "zip", "phone", "website", "email", "hours", "categories",
                "description", "service_area"]


def directories_for(vertical: str | None) -> list[tuple[str, str]]:
    return [(n, s) for n, s, v in DIRECTORIES if v is None or v == vertical]


def profile(company_id: int) -> dict:
    row = db.one("SELECT data FROM citation_profiles WHERE company_id = ?", company_id)
    if row:
        return db.loads(row["data"], {})
    co = db.one("SELECT * FROM companies WHERE id = ?", company_id)
    place = places_store.get(co["place_id"]) if co and co["place_id"] else None
    return profile_from(co["name"], (place or {}).get("address") or co.get("address"), co.get("phone") or (place or {}).get("phone"),
                        co.get("website") or (place or {}).get("website"), (place or {}).get("hours"),
                        (place or {}).get("primary_category"))


def profile_from(name, address, phone, website, hours=None, category=None) -> dict:
    parts = [p.strip() for p in (address or "").split(",")]
    street = parts[0] if parts else ""
    city = parts[1] if len(parts) > 2 else ""
    state_zip = parts[2] if len(parts) > 2 else ""
    m = re.match(r"([A-Za-z .]+?)\s*(\d{5}(?:-\d{4})?)?$", state_zip)
    return {"name": name or "", "street": street, "city": city, "state": (m.group(1).strip() if m else state_zip),
            "zip": (m.group(2) if m and m.group(2) else ""), "phone": phone or "", "website": website or "",
            "email": "", "hours": "; ".join(hours) if isinstance(hours, list) else (hours or ""),
            "categories": category or "", "description": "", "service_area": ""}


def save_profile(company_id: int, data: dict) -> None:
    clean = {k: (data.get(k) or "").strip() for k in PROFILE_KEYS}
    db.run("INSERT INTO citation_profiles (company_id, data, updated_at) VALUES (?, ?, ?) ON CONFLICT(company_id) "
           "DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at", company_id,
           __import__("json").dumps(clean), db.now())


# --- comparison (deterministic) -------------------------------------------------------------

def _digits(s) -> str:
    return re.sub(r"\D", "", s or "")[-10:]


def _norm(s) -> str:
    s = (s or "").lower()
    for a, b in [(" street", " st"), (" avenue", " ave"), (" road", " rd"), (" suite", " ste"), (" boulevard", " blvd"),
                 (" drive", " dr"), ("&", "and")]:
        s = s.replace(a, b)
    return re.sub(r"[^a-z0-9]", "", s)


def compare(nap: dict, listing: dict) -> dict:
    """Field-by-field: match | mismatch (with what was found) | absent | present | unknown."""
    out = {}
    name_l, name_p = _norm(listing.get("name")), _norm(nap.get("name"))
    out["name"] = {"status": "unknown"} if not listing.get("name") else \
        {"status": "match"} if name_l == name_p or (len(name_p) >= 6 and (name_p in name_l or name_l in name_p)) else \
        {"status": "mismatch", "found": listing.get("name")}
    addr = _norm(listing.get("address"))
    num = re.match(r"\s*(\d+)", nap.get("street") or "")
    zip_ok = not nap.get("zip") or nap["zip"][:5] in (listing.get("address") or "")
    num_ok = not num or num.group(1) in (listing.get("address") or "")
    street_ok = not nap.get("street") or _norm(nap["street"])[:12] in addr
    out["address"] = {"status": "absent", "found": "missing"} if not listing.get("address") else \
        {"status": "match"} if zip_ok and num_ok and street_ok else {"status": "mismatch", "found": listing.get("address")}
    if nap.get("phone"):
        out["phone"] = {"status": "absent", "found": "missing"} if not listing.get("phone") else \
            {"status": "match"} if _digits(listing["phone"]) == _digits(nap["phone"]) else {"status": "mismatch", "found": listing["phone"]}
    if nap.get("website"):
        out["website"] = {"status": "absent", "found": "missing"} if not listing.get("website") else \
            {"status": "match"} if domain(listing["website"]) == domain(nap["website"]) else {"status": "mismatch", "found": listing["website"]}
    for k in ("hours", "categories", "photos", "description"):
        v = listing.get(k)
        out[k] = {"status": "unknown"} if v is None else {"status": "present"} if v else {"status": "absent"}
    return out


def listing_status(fields: dict) -> str:
    core = [fields.get(k, {}).get("status") for k in ("name", "address", "phone", "website") if k in fields]
    return "wrong" if any(s in ("mismatch", "absent") for s in core) else "live"


def score(listings: list[dict]) -> tuple[int | None, str | None]:
    live = sum(1 for l in listings if l["status"] == "live")
    wrong = sum(1 for l in listings if l["status"] == "wrong")
    missing = sum(1 for l in listings if l["status"] == "missing")
    total = live + wrong + missing
    if not total:
        return None, None
    s = round((live + 0.5 * wrong) / total * 100)
    g = "A" if s >= 90 else "B" if s >= 80 else "C" if s >= 70 else "D" if s >= 60 else "F"
    return s, g


# --- audit run --------------------------------------------------------------------------------

def lookup_prompt(nap: dict, dirs: list[tuple[str, str]]) -> str:
    who = f"{nap['name']}, {nap.get('street', '')}, {nap.get('city', '')} {nap.get('state', '')} {nap.get('zip', '')}, phone {nap.get('phone', '')}"
    names = "\n".join(f"- {n} ({s})" for n, s in dirs)
    return (f"Find the business listing for this exact business on each directory below using web search, then open "
            f"the listing page to read it.\nBusiness: {who}\nDirectories:\n{names}\n\n"
            "Only report what you actually saw on the listing page. If you cannot find a listing for this business, "
            "set found to false. Never guess or fill in values from the business details above.\n"
            'Reply with a JSON list, one object per directory: {"directory": "...", "found": true|false, "url": "...", '
            '"name": "...", "address": "...", "phone": "...", "website": "...", "hours": true|false|null, '
            '"categories": true|false|null, "photos": true|false|null, "description": true|false|null, '
            '"claimed": true|false|null}')


def run_audit(audit_id: int, batch: int = 5) -> None:
    if not db.claim("citation_audits", audit_id, ("queue",), "in_progress"):
        return
    try:
        _run_audit(audit_id, batch)
    except Exception:
        db.run("UPDATE citation_listings SET status = 'unavailable' WHERE audit_id = ? AND status = 'pending'", audit_id)
        finish(audit_id)
        raise


def start(audit_id: int) -> None:
    """Queue automatic checks when the claude CLI is available; otherwise the audit is filled in by hand."""
    if ai.available():
        jobs.submit(run_audit, audit_id)
    else:
        db.update("citation_audits", audit_id, status="manual")


def _run_audit(audit_id: int, batch: int = 5) -> None:
    a = db.one("SELECT * FROM citation_audits WHERE id = ?", audit_id)
    nap = db.loads(a["business"], {})
    rows = db.all("SELECT * FROM citation_listings WHERE audit_id = ? AND status = 'pending'", audit_id)
    by_name = {r["directory"]: r for r in rows}
    dirs = [(n, s) for n, s, _ in DIRECTORIES if n in by_name]
    for i in range(0, len(dirs), batch):
        chunk = dirs[i:i + batch]
        try:
            found = ai.ask_json(lookup_prompt(nap, chunk), web=True, timeout=420)
        except Exception as exc:
            for n, _ in chunk:
                db.update("citation_listings", by_name[n]["id"], status="unavailable", checked_by="auto",
                          fields={"error": {"status": "unknown", "found": str(exc)[:200]}}, updated_at=db.now())
            continue
        results = {str(f.get("directory", "")).lower(): f for f in (found if isinstance(found, list) else [])}
        for n, _ in chunk:
            f = results.get(n.lower()) or next((v for k, v in results.items() if n.lower() in k or k in n.lower()), None)
            row = by_name[n]
            if not f or not f.get("found"):
                db.update("citation_listings", row["id"], status="missing", checked_by="auto", updated_at=db.now())
                continue
            fields = compare(nap, f)
            db.update("citation_listings", row["id"], status=listing_status(fields), listing_url=f.get("url"),
                      fields=fields, claimable=1 if f.get("claimed") is False else 0 if f.get("claimed") else None,
                      checked_by="auto", updated_at=db.now())
    finish(audit_id)


def finish(audit_id: int) -> None:
    listings = db.all("SELECT status FROM citation_listings WHERE audit_id = ?", audit_id)
    s, g = score(listings)
    done = all(l["status"] != "pending" for l in listings)
    current = db.one("SELECT status FROM citation_audits WHERE id = ?", audit_id)["status"]
    if done:
        status = "completed"
    elif current in ("manual", "completed"):
        status = "manual"  # filled in by hand: never show the "checking" spinner
    else:
        status = "in_progress"
    db.update("citation_audits", audit_id, score=s, grade=g, status=status, completed_at=db.now() if done else None)


def create_audit(nap: dict, *, company_id: int | None = None, place_id: int | None = None,
                 vertical: str | None = None) -> int:
    aid = db.insert("citation_audits", company_id=company_id, place_id=place_id, business=nap, status="queue",
                    created_at=db.now())
    for name, _site in directories_for(vertical):
        db.insert("citation_listings", audit_id=aid, company_id=company_id, directory=name, status="pending",
                  submission_status="todo", updated_at=db.now())
    return aid


def summary(listings: list[dict]) -> dict:
    field_acc = {f: {"ok": 0, "err": 0, "miss": 0} for f in FIELDS}
    for l in listings:
        for f, v in (db.loads(l.get("fields"), {}) or {}).items():
            bucket = {"match": "ok", "present": "ok", "mismatch": "err", "absent": "miss"}.get(v.get("status"))
            if f in field_acc and bucket:
                field_acc[f][bucket] += 1
    counts = {s: sum(1 for l in listings if l["status"] == s) for s in ("live", "wrong", "missing", "pending", "unavailable")}
    return {"counts": counts, "fields": field_acc, "claimable": sum(1 for l in listings if l.get("claimable") == 1)}


@jobs.on_startup
def recover_audits() -> None:
    db.run("UPDATE citation_audits SET status = 'queue' WHERE status = 'in_progress'")
    for a in db.all("SELECT id FROM citation_audits WHERE status = 'queue'"):
        start(a["id"])
