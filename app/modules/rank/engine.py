"""Rank scans (A1–A4): run a keyword search from every grid point, record where the target
ranks, keep every business seen, and compute the competitor statistics LeadSnap shows.

Formulas (shown in the UI next to each number):
  avg rank (found)  = mean rank over points where the business is in the top N
  avg rank (all)    = mean rank over all points, counting "not found" as N + 1
  top-3 %           = points ranked 1–3 / all points
  share of top 3    = top-3 appearances / (3 × points)   (how much of the map-pack space it holds)
"""
import calendar
import re
import secrets
from datetime import datetime, timedelta, timezone

from app import db, jobs, journal, places_store, settings
from app.modules.rank import grid
from app.providers import dataforseo, demo, places

DEPTH = 20
NOT_FOUND = DEPTH + 1

PROVIDERS = {
    "places": {"label": "Google Places API (approximate)", "surfaces": ["google_maps"]},
    "dataforseo": {"label": "DataForSEO SERP (per-point)", "surfaces": ["google_maps", "local_pack", "organic"]},
    "demo": {"label": "Demo data (fake businesses)", "surfaces": ["google_maps", "local_pack", "organic"]},
}
SURFACES = {"google_maps": "Google Maps", "local_pack": "Local pack", "organic": "Organic"}


def available_providers() -> dict:
    mods = settings.modules()
    return {k: v for k, v in PROVIDERS.items()
            if k == "demo" or (k == "places" and mods["places"]) or (k == "dataforseo" and mods["dataforseo"])}


# --- matching ------------------------------------------------------------------------

def _norm_name(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _domain(url: str | None) -> str:
    m = re.match(r"^(?:https?://)?(?:www\.)?([^/:?#]+)", (url or "").lower())
    return m.group(1) if m else ""


def business_key(b: dict) -> str:
    return b.get("google_place_id") or (f"cid:{b['cid']}" if b.get("cid") else f"name:{_norm_name(b.get('name'))}")


def find_rank(target: dict, results: list[dict], surface: str = "google_maps") -> int | None:
    """1-based position of target in results, or None if absent."""
    tdom = _domain(target.get("website"))
    for i, r in enumerate(results, start=1):
        if target.get("google_place_id") and r.get("google_place_id") == target["google_place_id"]:
            return i
        if target.get("cid") and r.get("cid") and str(r["cid"]) == str(target["cid"]):
            return i
        if surface == "organic":
            if tdom and _domain(r.get("website") or r.get("domain")) == tdom:
                return i
        elif target.get("name") and _norm_name(r.get("name")) == _norm_name(target["name"]):
            # Name is only a fallback when IDs can't be compared; otherwise another branch of a
            # chain with the same name would be counted as the target.
            both_have_ids = (target.get("google_place_id") and r.get("google_place_id")) or \
                            (target.get("cid") and r.get("cid"))
            if not both_have_ids:
                return i
    return None


# --- searching ---------------------------------------------------------------------

def provider_search(provider: str, surface: str, keyword: str, lat: float, lng: float,
                    spacing_m: float, center: tuple[float, float], target: dict | None) -> list[dict]:
    if provider == "demo":
        res = demo.search(keyword, lat, lng, center, extra=target, depth=DEPTH)
        return res[:3] if surface == "local_pack" else res
    if provider == "places":
        if surface != "google_maps":
            raise ValueError("Google Places mode only supports the Google Maps surface.")
        return places.text_search(keyword, lat, lng, radius_m=max(spacing_m, 500), max_results=DEPTH,
                                  fields=places.SCAN_FIELDS)
    if provider == "dataforseo":
        if surface == "google_maps":
            return dataforseo.maps_search(keyword, lat, lng, spacing_m, depth=DEPTH)
        serp = dataforseo.organic_search(keyword, lat, lng, depth=DEPTH)
        return serp["local_pack"] if surface == "local_pack" else serp["organic"]
    raise ValueError(f"Unknown provider {provider}")


def create_scans(place_id: int | None, keywords: list[str], *, surface: str, provider: str,
                 size: int, spacing_m: float, shape: str = "square", polygon=None,
                 center: tuple[float, float] | None = None, schedule_id: int | None = None,
                 run_now: bool = True) -> list[int]:
    if provider not in PROVIDERS:
        raise ValueError("Unknown provider")
    if provider not in available_providers():
        raise ValueError(f"{PROVIDERS[provider]['label']} is not configured (add its key to .env).")
    if surface not in PROVIDERS[provider]["surfaces"]:
        raise ValueError(f"{PROVIDERS[provider]['label']} does not support {SURFACES.get(surface, surface)}.")
    target = places_store.get(place_id) if place_id else None
    if center is None:
        if not target or target.get("lat") is None:
            raise ValueError("Pick a business with a location or give a grid centre.")
        center = (target["lat"], target["lng"])
    ids = []
    for kw in [k.strip() for k in keywords if k.strip()]:
        sid = db.insert("scans", place_id=place_id, schedule_id=schedule_id, keyword=kw, surface=surface,
                        provider=provider, grid_size=size, spacing_m=spacing_m, shape=shape,
                        center_lat=center[0], center_lng=center[1], status="queue",
                        stats={"polygon": polygon} if polygon else None, created_at=db.now())
        ids.append(sid)
        if run_now:
            jobs.submit(run_scan, sid)
    return ids


def run_scan(scan_id: int) -> None:
    if not db.claim("scans", scan_id, ("queue",), "in_progress"):
        return  # missing, or already taken by another worker
    try:
        _run_scan(scan_id)
    except Exception as exc:
        db.update("scans", scan_id, status="failed", error=f"Scan stopped: {exc}"[:500], completed_at=db.now())
        raise


def _run_scan(scan_id: int) -> None:
    scan = db.one("SELECT * FROM scans WHERE id = ?", scan_id)
    db.update("scans", scan_id, error=None)
    db.run("DELETE FROM scan_points WHERE scan_id = ?", scan_id)
    polygon = (db.loads(scan["stats"], {}) or {}).get("polygon")
    target_row = places_store.get(scan["place_id"]) if scan["place_id"] else None
    target = places_store.as_target(target_row) if target_row else None
    center = (scan["center_lat"], scan["center_lng"])
    pts = grid.generate(*center, scan["grid_size"], scan["spacing_m"], scan["shape"] or "square", polygon)
    errors = []
    ok_points = 0
    for p in pts:
        try:
            results = provider_search(scan["provider"], scan["surface"], scan["keyword"], p["lat"], p["lng"],
                                      scan["spacing_m"], center, target)
        except Exception as exc:  # keep going; a scan with gaps is "incomplete"
            errors.append(str(exc))
            db.insert("scan_points", scan_id=scan_id, idx=p["idx"], lat=p["lat"], lng=p["lng"], rank=None,
                      results=None)
            if len(errors) >= 3 and len(errors) == p["idx"] + 1:
                break  # first points all failing: provider is down or misconfigured
            continue
        rank = find_rank(target, results, scan["surface"]) if target else None
        slim = [{k: r.get(k) for k in ("google_place_id", "cid", "name", "address", "rating", "review_count",
                                      "primary_category", "categories", "website", "maps_url", "photos_count",
                                      "lat", "lng")} for r in results]
        db.insert("scan_points", scan_id=scan_id, idx=p["idx"], lat=p["lat"], lng=p["lng"], rank=rank,
                  results=slim)
        ok_points += 1
    status = "completed" if ok_points == len(pts) else ("failed" if ok_points == 0 else "incomplete")
    stats = target_stats(scan_id) if ok_points else {}
    if polygon:
        stats["polygon"] = polygon
    db.update("scans", scan_id, status=status, stats=stats,
              error=(f"{len(errors)} of {len(pts)} points failed: {errors[0]}" if errors else None),
              completed_at=db.now())
    journal.record("rank", "job", "scan", "ok" if status == "completed" else "failed", subject_type="scan",
                   subject_id=scan_id, detail=f"{scan['provider']} {status}: {ok_points}/{len(pts)} points",
                   data={"provider": scan["provider"], "points": len(pts), "failed_points": len(errors),
                         "first_error": errors[0] if errors else None})
    if scan["provider"] == "dataforseo" and ok_points:
        jobs.submit(refresh_keyword_data, [scan["keyword"]])
    if ok_points:
        from app.modules.rank import hooks
        jobs.submit(hooks.scan_completed, scan_id)  # vault export problems must not fail the scan


def refresh_keyword_data(keywords: list[str]) -> None:
    for row in dataforseo.keyword_overview(keywords):
        db.run("INSERT INTO keyword_data (keyword, search_volume, cpc, competition, difficulty, intent, fetched_at) "
               "VALUES (?,?,?,?,?,?,?) ON CONFLICT(keyword) DO UPDATE SET search_volume=excluded.search_volume, "
               "cpc=excluded.cpc, competition=excluded.competition, difficulty=excluded.difficulty, "
               "intent=excluded.intent, fetched_at=excluded.fetched_at",
               row["keyword"], row["search_volume"], row["cpc"], row["competition"], row["difficulty"],
               row["intent"], db.now())


# --- statistics ----------------------------------------------------------------------

def points(scan_id: int) -> list[dict]:
    rows = db.all("SELECT * FROM scan_points WHERE scan_id = ? ORDER BY idx", scan_id)
    for r in rows:
        r["results"] = db.loads(r["results"], None)
    return rows


def _summary(ranks: list[int | None], pts: list[dict], center: tuple[float, float]) -> dict:
    n = len(ranks)
    found = [r for r in ranks if r]
    top3 = sum(1 for r in found if r <= 3)
    quad: dict[str, list[int]] = {"nw": [], "ne": [], "sw": [], "se": []}
    for r, p in zip(ranks, pts):
        if p["lat"] == center[0] or p["lng"] == center[1]:
            continue  # points on the centre lines belong to no quadrant
        q = ("n" if p["lat"] > center[0] else "s") + ("w" if p["lng"] < center[1] else "e")
        quad[q].append(r or NOT_FOUND)
    return {
        "points": n,
        "found_points": len(found),
        "top3_points": top3,
        "avg_rank": round(sum(found) / len(found), 2) if found else None,
        "avg_rank_all": round(sum(r or NOT_FOUND for r in ranks) / n, 2) if n else None,
        "top3_pct": round(100 * top3 / n, 1) if n else 0,
        "share_top3": round(100 * top3 / (3 * n), 1) if n else 0,
        "quadrants": {k: (round(sum(v) / len(v), 2) if v else None) for k, v in quad.items()},
    }


def target_stats(scan_id: int) -> dict:
    scan = db.one("SELECT * FROM scans WHERE id = ?", scan_id)
    pts = [p for p in points(scan_id) if p["results"] is not None]
    return _summary([p["rank"] for p in pts], pts, (scan["center_lat"], scan["center_lng"]))


def competitors(scan_id: int) -> list[dict]:
    """Every business seen anywhere in the scan, with its own grid statistics (flip the map)."""
    scan = db.one("SELECT * FROM scans WHERE id = ?", scan_id)
    pts = [p for p in points(scan_id) if p["results"] is not None]
    center = (scan["center_lat"], scan["center_lng"])
    seen: dict[str, dict] = {}
    for p in pts:
        for r in p["results"]:
            seen.setdefault(business_key(r), r)
    target = places_store.get(scan["place_id"]) if scan["place_id"] else None
    target_key = business_key(target) if target else None
    rows = []
    for key, biz in seen.items():
        ranks = []
        for p in pts:
            rank = next((i for i, r in enumerate(p["results"], 1) if business_key(r) == key), None)
            ranks.append(rank)
        s = _summary(ranks, pts, center)
        rows.append({"key": key, "business": biz, "ranks": ranks, "you": key == target_key, **s})
    rows.sort(key=lambda r: (r["avg_rank_all"] or 99, -r["found_points"]))
    for i, r in enumerate(rows, 1):
        r["position"] = i
    by_share = sorted(rows, key=lambda r: -r["share_top3"])
    for i, r in enumerate(by_share, 1):
        r["share_position"] = i
    return rows


def previous_scan(scan: dict) -> dict | None:
    # Only a like-for-like scan: a 9×9 Places grid compared with a 3×3 demo grid would show a fake change.
    return db.one("SELECT * FROM scans WHERE place_id IS ? AND keyword = ? AND surface = ? AND provider = ? AND "
                  "grid_size = ? AND shape IS ? AND status IN ('completed','incomplete') AND id < ? ORDER BY id DESC LIMIT 1",
                  scan["place_id"], scan["keyword"], scan["surface"], scan["provider"], scan["grid_size"], scan["shape"],
                  scan["id"])


def history(place_id: int, keyword: str, surface: str) -> list[dict]:
    rows = db.all("SELECT id, stats, completed_at FROM scans WHERE place_id = ? AND keyword = ? AND surface = ? "
                  "AND status IN ('completed','incomplete') ORDER BY id", place_id, keyword, surface)
    return [{"id": r["id"], "date": (r["completed_at"] or "")[:10], **(db.loads(r["stats"], {}) or {})} for r in rows]


def ensure_share_token(scan_id: int) -> str:
    scan = db.one("SELECT share_token FROM scans WHERE id = ?", scan_id)
    if scan and scan["share_token"]:
        return scan["share_token"]
    token = secrets.token_urlsafe(16)
    db.update("scans", scan_id, share_token=token)
    return token


def request_count(size: int, shape: str, keywords: int) -> int:
    return len(grid.generate(0.0, 0.0, size, 1000, shape)) * max(keywords, 1)


# --- schedules (A3) ------------------------------------------------------------------

RECURRENCES = {"daily": "Daily", "weekly": "Weekly", "biweekly": "Every 14 days",
               "monthly_day": "Monthly on a set day", "first_monday": "First Monday of the month"}


def next_run(recurrence: str, day: int | None, after: datetime) -> datetime:
    at = after.replace(hour=6, minute=0, second=0, microsecond=0)
    if recurrence == "daily":
        return at + timedelta(days=1) if at <= after else at
    if recurrence in ("weekly", "biweekly"):
        wd = 0 if day is None else int(day)
        delta = (wd - at.weekday()) % 7
        cand = at + timedelta(days=delta)
        if cand <= after:
            cand += timedelta(days=7 if recurrence == "weekly" else 14)
        return cand
    if recurrence in ("monthly_day", "first_monday"):
        y, m = at.year, at.month
        for _ in range(3):
            if recurrence == "monthly_day":
                d = min(int(day or 1), calendar.monthrange(y, m)[1])
            else:
                d = next(dd for dd in range(1, 8) if datetime(y, m, dd).weekday() == 0)
            cand = at.replace(year=y, month=m, day=d)
            if cand > after:
                return cand
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    raise ValueError(f"Unknown recurrence {recurrence}")


def run_schedule(schedule_id: int) -> list[int]:
    s = db.one("SELECT * FROM scan_schedules WHERE id = ?", schedule_id)
    g = db.loads(s["grid"], {})
    now = datetime.now(timezone.utc)
    nxt = next_run(s["recurrence"], s["day"], now)
    if s["recurrence"] == "biweekly" and s["next_run_at"]:
        # Keep the 14-day rhythm when this run was late (app closed): don't snap to the next matching weekday.
        nxt = datetime.fromisoformat(s["next_run_at"]) + timedelta(days=14)
        while nxt <= now:
            nxt += timedelta(days=14)
    # Advance first, so a failing schedule (e.g. a removed API key) is retried next period, not every minute.
    db.update("scan_schedules", schedule_id, last_run_at=now.isoformat(timespec="seconds"),
              next_run_at=nxt.isoformat(timespec="seconds"))
    try:
        ids = create_scans(s["place_id"], db.loads(s["keywords"], []), surface=s["surface"], provider=s["provider"],
                           size=g["size"], spacing_m=g["spacing_m"], shape=g.get("shape", "square"),
                           polygon=g.get("polygon"), schedule_id=schedule_id)
    except ValueError as exc:
        db.update("scan_schedules", schedule_id, last_error=str(exc))
        journal.failure("rank", "schedule", exc, subject_type="schedule", subject_id=schedule_id)
        raise
    db.update("scan_schedules", schedule_id, last_error=None)
    return ids


@jobs.on_tick
def run_due_schedules() -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for s in db.all("SELECT id FROM scan_schedules WHERE paused = 0 AND next_run_at <= ?", now):
        try:
            run_schedule(s["id"])
        except ValueError:
            continue  # recorded on the schedule (last_error) and in the journal; the others still run


@jobs.on_tick
def run_queued() -> None:
    """Scans queued by another process (the MCP server while the app is running) run here, so only one process
    ever runs scans. run_scan claims each scan, so one also submitted directly can't run twice."""
    for s in db.all("SELECT id FROM scans WHERE status = 'queue' AND created_at <= ?",
                    (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat(timespec="seconds")):
        jobs.submit(run_scan, s["id"])


@jobs.on_startup
def recover_scans() -> None:
    """Scans cut off by a crash or the app closing restart from the beginning."""
    db.run("UPDATE scans SET status = 'queue' WHERE status = 'in_progress'")
    for s in db.all("SELECT id FROM scans WHERE status = 'queue'"):
        jobs.submit(run_scan, s["id"])
