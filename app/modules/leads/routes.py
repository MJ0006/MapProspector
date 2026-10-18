"""Find leads: search Google for businesses in a niche + area, filter for opportunity, save as leads."""
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app import db, places_store, settings
from app.providers import demo, places
from app.web import redirect, render, safe_back

router = APIRouter()


def _median(values: list[float]) -> float | None:
    v = sorted(x for x in values if x is not None)
    if not v:
        return None
    mid = len(v) // 2
    return v[mid] if len(v) % 2 else (v[mid - 1] + v[mid]) / 2


def search_businesses(niche: str, area: str, lat: float | None = None, lng: float | None = None) -> list[dict]:
    if settings.enabled("places"):
        return places.text_search(f"{niche} in {area}" if area else niche, lat, lng, max_results=60)
    if lat is None or lng is None:
        raise ValueError("Without a Google key, enter coordinates to see demo businesses.")
    return demo.businesses(niche, lat, lng)


def opportunity_flags(b: dict, median_reviews: float | None) -> list[str]:
    """Plain, factual reasons a business may need help (no made-up score)."""
    flags = []
    if not b.get("website"):
        flags.append("No website")
    if b.get("rating") is not None and b["rating"] < 4.0:
        flags.append(f"Rating {b['rating']}")
    if median_reviews and (b.get("review_count") or 0) < median_reviews / 2:
        flags.append("Few reviews vs. area median")
    if b.get("photos_count") is not None and b["photos_count"] < 5:
        flags.append("Few photos")
    if not b.get("hours") and settings.enabled("places"):
        flags.append("No hours listed")
    return flags


@router.get("/leads/search")
def leads_search(request: Request, niche: str = "", area: str = "", lat: str = "", lng: str = "",
                 no_website: int = 0, max_rating: str = "", max_reviews: str = ""):
    results, error, median_reviews = [], "", None
    if niche:
        try:
            results = search_businesses(niche, area, float(lat) if lat else None, float(lng) if lng else None)
        except Exception as exc:
            error = str(exc)
        median_reviews = _median([r.get("review_count") for r in results])
        for r in results:
            r["flags"] = opportunity_flags(r, median_reviews)
        if no_website:
            results = [r for r in results if not r.get("website")]
        if max_rating:
            results = [r for r in results if r.get("rating") is None or r["rating"] <= float(max_rating)]
        if max_reviews:
            results = [r for r in results if (r.get("review_count") or 0) <= int(max_reviews)]
        saved = {r["google_place_id"] for r in db.all("SELECT p.google_place_id FROM contacts c JOIN places p "
                                                       "ON p.id = c.place_id")}
        for r in results:
            r["saved"] = r.get("google_place_id") in saved
    return render(request, "leads/search.html", title="Find leads", results=results, error=error,
                  q={"niche": niche, "area": area, "lat": lat, "lng": lng, "no_website": no_website,
                     "max_rating": max_rating, "max_reviews": max_reviews}, median_reviews=median_reviews)


@router.post("/leads/save-place")
async def leads_save_place(request: Request):
    """Save a search result (sent as form fields) as a place + lead contact."""
    form = dict(await request.form())
    b = {k: (form.get(k) or None) for k in ("google_place_id", "name", "address", "phone", "website",
                                             "primary_category", "maps_url")}
    for k in ("lat", "lng", "rating"):
        b[k] = float(form[k]) if form.get(k) else None
    for k in ("review_count", "photos_count"):
        b[k] = int(form[k]) if form.get(k) else None
    pid = places_store.upsert(b)
    cid = save_lead(pid)
    then_scan = form.get("then") == "scan" or request.query_params.get("then") == "scan"
    if request.headers.get("HX-Request"):
        if then_scan:
            return HTMLResponse("", headers={"HX-Redirect": f"/rank/new?place_id={pid}"})
        return HTMLResponse(f'<a class="badge good" href="/crm/contacts/{cid}">saved</a>')
    if then_scan:
        return redirect(f"/rank/new?place_id={pid}")
    return redirect(safe_back(form.get("back"), f"/crm/contacts/{cid}"))


def save_lead(place_id: int) -> int:
    existing = db.one("SELECT id FROM contacts WHERE place_id = ?", place_id)
    if existing:
        return existing["id"]
    p = places_store.get(place_id)
    cid = db.insert("contacts", place_id=place_id, business_name=p["name"], phone=p.get("phone"),
                    website=p.get("website"), address=p.get("address"), source="prospecting", status="new",
                    tags=[], created_at=db.now(), last_activity_at=db.now(), last_activity_type="created")
    db.insert("activities", contact_id=cid, type="note", body="Saved from lead search.", created_at=db.now())
    from app.modules.crm import automations
    automations.fire("contact_created", cid)
    return cid


@router.get("/leads/save")
def leads_save(place_id: int):
    if not places_store.get(place_id):
        raise HTTPException(404)
    return redirect(f"/crm/contacts/{save_lead(place_id)}")


@router.get("/api/places/search")
def api_places_search(q: str, lat: float | None = None, lng: float | None = None):
    if not settings.enabled("places"):
        return JSONResponse({"error": "Set GOOGLE_API_KEY to search Google."}, status_code=400)
    try:
        found = places.text_search(q, lat, lng, max_results=8)
    except places.PlacesError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    out = []
    for b in found:
        pid = places_store.upsert(b)
        out.append({"id": pid, "name": b["name"], "address": b["address"], "lat": b["lat"], "lng": b["lng"]})
    return {"results": out}


@router.get("/api/places/{place_id}")
def api_place(place_id: int):
    p = places_store.get(place_id)
    if not p:
        raise HTTPException(404)
    return p
