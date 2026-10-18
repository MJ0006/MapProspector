from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import JSONResponse

from app import db, places_store
from app.modules.rank import engine, grid
from app.web import redirect, render

router = APIRouter()


def _scan_or_404(scan_id: int) -> dict:
    scan = db.one("SELECT * FROM scans WHERE id = ?", scan_id)
    if not scan:
        raise HTTPException(404, "Scan not found")
    return scan


@router.get("/rank")
def rank_list(request: Request):
    scans = db.all("SELECT s.*, p.name business FROM scans s LEFT JOIN places p ON p.id = s.place_id "
                   "ORDER BY s.id DESC LIMIT 200")
    schedules = db.all("SELECT s.*, p.name business FROM scan_schedules s LEFT JOIN places p ON p.id = s.place_id "
                       "ORDER BY s.id DESC")
    return render(request, "rank/list.html", title="Rank tracker", scans=scans, schedules=schedules,
                  providers=engine.PROVIDERS, surfaces=engine.SURFACES, recurrences=engine.RECURRENCES)


@router.get("/rank/new")
def rank_new(request: Request, place_id: int | None = None, error: str = ""):
    place = places_store.get(place_id) if place_id else None
    saved = db.all("SELECT id, name, address FROM places ORDER BY name LIMIT 500")
    return render(request, "rank/new.html", title="New rank scan", place=place, saved=saved, error=error,
                  providers=engine.available_providers(), surfaces=engine.SURFACES,
                  recurrences=engine.RECURRENCES)


@router.post("/rank/new")
def rank_create(place_id: str = Form(""), manual_name: str = Form(""), manual_lat: str = Form(""),
                manual_lng: str = Form(""), manual_website: str = Form(""),
                keywords: str = Form(...), surface: str = Form("google_maps"), provider: str = Form("demo"),
                size: int = Form(7), spacing: float = Form(1.0), unit: str = Form("mi"),
                shape: str = Form("square"), recurrence: str = Form(""), day: str = Form(""),
                schedule_name: str = Form("")):
    try:
        if place_id:
            pid = int(place_id)
        elif manual_name and manual_lat and manual_lng:
            pid = places_store.upsert({"name": manual_name.strip(), "lat": float(manual_lat),
                                       "lng": float(manual_lng), "website": manual_website or None})
        else:
            raise ValueError("Choose a business, or enter a name and coordinates.")
        spacing_m = spacing * (grid.MILE_M if unit == "mi" else 1000)
        kws = [k.strip() for k in keywords.replace("\n", ",").split(",") if k.strip()]
        if not kws:
            raise ValueError("Enter at least one keyword.")
        if recurrence:
            now = datetime.now(timezone.utc)
            sid = db.insert("scan_schedules", name=schedule_name or f"{', '.join(kws)[:60]}", place_id=pid,
                            keywords=kws, surface=surface, provider=provider,
                            grid={"size": size, "spacing_m": spacing_m, "shape": shape},
                            recurrence=recurrence, day=int(day) if day else None, paused=0,
                            next_run_at=engine.next_run(recurrence, int(day) if day else None, now)
                            .isoformat(timespec="seconds"), created_at=db.now())
            ids = engine.run_schedule(sid)
        else:
            ids = engine.create_scans(pid, kws, surface=surface, provider=provider, size=size,
                                      spacing_m=spacing_m, shape=shape)
    except (ValueError, KeyError) as exc:
        q = f"place_id={quote(place_id)}&" if place_id else ""
        return redirect(f"/rank/new?{q}error={quote(str(exc))}")
    return redirect(f"/rank/{ids[0]}" if len(ids) == 1 else "/rank")


@router.get("/api/grid-preview")
def grid_preview(lat: float, lng: float, size: int = 7, spacing: float = 1.0, unit: str = "mi",
                 shape: str = "square", keywords: int = 1):
    spacing_m = spacing * (grid.MILE_M if unit == "mi" else 1000)
    try:
        pts = grid.generate(lat, lng, size, spacing_m, shape)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return {"points": pts, "requests": len(pts) * max(keywords, 1)}


def _scan_context(scan: dict) -> dict:
    stats = db.loads(scan["stats"], {}) or {}
    place = places_store.get(scan["place_id"]) if scan["place_id"] else None
    prev = engine.previous_scan(scan)
    prev_stats = db.loads(prev["stats"], {}) if prev else None
    change = None
    if prev_stats and stats.get("avg_rank_all") is not None and prev_stats.get("avg_rank_all") is not None:
        change = round(prev_stats["avg_rank_all"] - stats["avg_rank_all"], 2)  # positive = improved
    comps = engine.competitors(scan["id"]) if scan["status"] in ("completed", "incomplete") else []
    kw = db.one("SELECT * FROM keyword_data WHERE keyword = ?", scan["keyword"])
    hist = engine.history(scan["place_id"], scan["keyword"], scan["surface"]) if scan["place_id"] else []
    return {"scan": scan, "stats": stats, "place": place, "prev": prev, "change": change,
            "competitors": comps, "kw": kw, "history": hist, "surfaces": engine.SURFACES,
            "providers": engine.PROVIDERS, "not_found": engine.NOT_FOUND}


@router.get("/rank/{scan_id}")
def rank_detail(request: Request, scan_id: int):
    scan = _scan_or_404(scan_id)
    return render(request, "rank/detail.html", title=f"Scan: {scan['keyword']}", **_scan_context(scan))


@router.get("/rank/{scan_id}/status")
def rank_status(scan_id: int):
    scan = _scan_or_404(scan_id)
    done = db.one("SELECT COUNT(*) n FROM scan_points WHERE scan_id = ?", scan_id)["n"]
    return {"status": scan["status"], "points_done": done, "error": scan["error"]}


@router.get("/rank/{scan_id}/data")
def rank_data(scan_id: int):
    scan = _scan_or_404(scan_id)
    return _data(scan)


def _data(scan: dict) -> dict:
    pts = engine.points(scan["id"])
    comps = engine.competitors(scan["id"]) if scan["status"] in ("completed", "incomplete") else []
    return {
        "center": [scan["center_lat"], scan["center_lng"]],
        "points": [{"lat": p["lat"], "lng": p["lng"], "rank": p["rank"], "ok": p["results"] is not None,
                    "top": [r["name"] for r in (p["results"] or [])[:3]]} for p in pts],
        "competitors": [{"key": c["key"], "name": c["business"]["name"], "you": c["you"], "ranks": c["ranks"],
                         "avg_rank": c["avg_rank"], "avg_rank_all": c["avg_rank_all"], "top3_pct": c["top3_pct"]}
                        for c in comps[:60]],
        "polygon": (db.loads(scan["stats"], {}) or {}).get("polygon"),
    }


@router.post("/rank/{scan_id}/rerun")
def rank_rerun(scan_id: int):
    scan = _scan_or_404(scan_id)
    try:
        ids = engine.create_scans(scan["place_id"], [scan["keyword"]], surface=scan["surface"],
                                  provider=scan["provider"], size=scan["grid_size"], spacing_m=scan["spacing_m"],
                                  shape=scan["shape"], center=(scan["center_lat"], scan["center_lng"]),
                                  polygon=(db.loads(scan["stats"], {}) or {}).get("polygon"))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return redirect(f"/rank/{ids[0]}")


@router.post("/rank/{scan_id}/delete")
def rank_delete(scan_id: int):
    db.run("DELETE FROM scans WHERE id = ?", scan_id)
    return redirect("/rank")


@router.post("/rank/{scan_id}/share")
def rank_share(scan_id: int):
    _scan_or_404(scan_id)
    engine.ensure_share_token(scan_id)
    return redirect(f"/rank/{scan_id}#share")


@router.get("/share/scan/{token}")
def rank_shared(request: Request, token: str):
    scan = db.one("SELECT * FROM scans WHERE share_token = ?", token)
    if not scan:
        raise HTTPException(404, "This link is not valid.")
    return render(request, "rank/share.html", title="Rank report", shared=True, token=token, **_scan_context(scan))


@router.get("/share/scan/{token}/data")
def rank_shared_data(token: str):
    scan = db.one("SELECT * FROM scans WHERE share_token = ?", token)
    if not scan:
        raise HTTPException(404)
    return _data(scan)


@router.post("/rank/schedules/{schedule_id}/{action}")
def schedule_action(schedule_id: int, action: str):
    s = db.one("SELECT * FROM scan_schedules WHERE id = ?", schedule_id)
    if not s:
        raise HTTPException(404)
    if action == "pause":
        db.update("scan_schedules", schedule_id, paused=1)
    elif action == "resume":
        nxt = engine.next_run(s["recurrence"], s["day"], datetime.now(timezone.utc))
        db.update("scan_schedules", schedule_id, paused=0, next_run_at=nxt.isoformat(timespec="seconds"))
    elif action == "run":
        try:
            engine.run_schedule(schedule_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
    elif action == "delete":
        db.run("DELETE FROM scan_schedules WHERE id = ?", schedule_id)
    return redirect("/rank")
