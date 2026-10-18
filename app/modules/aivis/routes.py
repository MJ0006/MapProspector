import secrets

from fastapi import APIRouter, Form, HTTPException, Request

from app import db, jobs, places_store
from app.modules.aivis import engine
from app.providers import ai_engines
from app.web import redirect, render

router = APIRouter()


@router.get("/aivis")
def aivis_list(request: Request):
    runs = db.all("SELECT * FROM aivis_runs ORDER BY id DESC LIMIT 100")
    return render(request, "aivis/list.html", title="AI visibility", runs=runs,
                  engines=[ai_engines.ENGINES[e] for e in ai_engines.available()])


@router.get("/aivis/new")
def aivis_new(request: Request, place_id: int | None = None):
    place = places_store.get(place_id) if place_id else None
    city = None
    if place and place.get("address"):
        parts = [p.strip() for p in place["address"].split(",")]
        city = ", ".join(parts[-3:-1]) if len(parts) >= 3 else parts[0]
    return render(request, "aivis/list.html", title="AI visibility", runs=[], place=place, city=city,
                  engines=[ai_engines.ENGINES[e] for e in ai_engines.available()])


@router.post("/aivis")
def aivis_create(business_name: str = Form(...), city: str = Form(...), category: str = Form(...),
                 place_id: str = Form(""), website: str = Form("")):
    pid = int(place_id) if place_id else None
    if not pid and website:
        pid = places_store.upsert({"name": business_name, "website": website})
    rid = db.insert("aivis_runs", place_id=pid, business_name=business_name.strip(), city=city.strip(),
                    category=category.strip(), status="queue", created_at=db.now())
    jobs.submit(engine.run, rid)
    return redirect(f"/aivis/{rid}")


def _run(rid: int) -> dict:
    r = db.one("SELECT * FROM aivis_runs WHERE id = ?", rid)
    if not r:
        raise HTTPException(404)
    return r


@router.get("/aivis/{rid}")
def aivis_view(request: Request, rid: int):
    r = _run(rid)
    return render(request, "aivis/report.html", title=f"AI visibility: {r['business_name']}", r=r,
                  report=db.loads(r["report"], None), engine_names=ai_engines.ENGINES)


@router.post("/aivis/{rid}/share")
def aivis_share(rid: int):
    r = _run(rid)
    if not r["share_token"]:
        db.update("aivis_runs", rid, share_token=secrets.token_urlsafe(16))
    return redirect(f"/aivis/{rid}#share")


@router.get("/share/aivis/{token}")
def aivis_shared(request: Request, token: str):
    r = db.one("SELECT * FROM aivis_runs WHERE share_token = ?", token)
    if not r:
        raise HTTPException(404)
    return render(request, "aivis/share.html", title="AI visibility report", r=r,
                  report=db.loads(r["report"], None), engine_names=ai_engines.ENGINES)
