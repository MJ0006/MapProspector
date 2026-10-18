"""DataForSEO: live Google Maps / organic SERP from a coordinate, plus keyword metrics.

Docs: https://docs.dataforseo.com/v3/serp/google/maps/live/advanced/
      https://docs.dataforseo.com/v3/serp/google/organic/live/advanced/
      https://docs.dataforseo.com/v3/dataforseo_labs/google/keyword_overview/live/
Billing is per task on your DataForSEO account.
"""
import httpx

from app import settings

BASE = "https://api.dataforseo.com/v3"


class DataForSEOError(RuntimeError):
    pass


def _auth() -> tuple[str, str]:
    login, pw = settings.env("DATAFORSEO_LOGIN"), settings.env("DATAFORSEO_PASSWORD")
    if not (login and pw):
        raise DataForSEOError("Set DATAFORSEO_LOGIN and DATAFORSEO_PASSWORD in .env.")
    return login, pw


def _post(path: str, payload: list, client: httpx.Client | None) -> dict:
    http = client or httpx.Client(timeout=90)
    try:
        r = http.post(f"{BASE}{path}", json=payload, auth=_auth())
    finally:
        if client is None:
            http.close()
    if r.status_code != 200:
        raise DataForSEOError(f"DataForSEO {path} failed ({r.status_code}): {r.text[:300]}")
    data = r.json()
    task = (data.get("tasks") or [{}])[0]
    if task.get("status_code") not in (20000, None):
        raise DataForSEOError(f"DataForSEO task error {task.get('status_code')}: {task.get('status_message')}")
    return task


def _zoom_for(spacing_m: float) -> int:
    # Tighter grids get a closer map viewport, like a person zoomed in on their area.
    return 15 if spacing_m <= 800 else 14 if spacing_m <= 2000 else 13 if spacing_m <= 5000 else 12


def _norm_maps_item(it: dict) -> dict:
    rating = it.get("rating") or {}
    return {
        "google_place_id": it.get("place_id"),
        "cid": it.get("cid"),
        "name": it.get("title", ""),
        "address": it.get("address"),
        "lat": it.get("latitude"),
        "lng": it.get("longitude"),
        "rating": rating.get("value"),
        "review_count": rating.get("votes_count"),
        "primary_category": it.get("category"),
        "categories": it.get("additional_categories") or [],
        "website": it.get("url"),
        "domain": it.get("domain"),
        "phone": it.get("phone"),
        "maps_url": f"https://maps.google.com/?cid={it['cid']}" if it.get("cid") else None,
        "photos_count": it.get("total_photos"),
    }


def maps_search(keyword: str, lat: float, lng: float, spacing_m: float = 1000, depth: int = 20,
                client: httpx.Client | None = None) -> list[dict]:
    """Google Maps results as seen from (lat, lng), in rank order."""
    task = _post("/serp/google/maps/live/advanced", [{
        "keyword": keyword, "language_code": "en", "depth": depth,
        "location_coordinate": f"{lat:.7f},{lng:.7f},{_zoom_for(spacing_m)}z",
    }], client)
    items = ((task.get("result") or [{}])[0] or {}).get("items") or []
    items = [i for i in items if i.get("type") == "maps_search"]
    items.sort(key=lambda i: i.get("rank_group") or 999)
    return [_norm_maps_item(i) for i in items[:depth]]


def organic_search(keyword: str, lat: float, lng: float, depth: int = 20,
                   client: httpx.Client | None = None) -> dict:
    """Organic SERP from (lat, lng): returns {'local_pack': [...], 'organic': [...]}."""
    task = _post("/serp/google/organic/live/advanced", [{
        "keyword": keyword, "language_code": "en", "depth": depth,
        "location_coordinate": f"{lat:.7f},{lng:.7f}",
    }], client)
    items = ((task.get("result") or [{}])[0] or {}).get("items") or []
    pack = [i for i in items if i.get("type") == "local_pack"]
    organic = sorted((i for i in items if i.get("type") == "organic"), key=lambda i: i.get("rank_group") or 999)
    return {
        "local_pack": [_norm_maps_item(i) for i in sorted(pack, key=lambda i: i.get("rank_group") or 999)],
        "organic": [{"name": i.get("title", ""), "website": i.get("url"), "domain": i.get("domain")}
                    for i in organic[:depth]],
    }


def ai_overview(keyword: str, lat: float, lng: float, client: httpx.Client | None = None) -> dict:
    """Google AI Overview seen from (lat, lng): {'present', 'text', 'references': [urls]}."""
    task = _post("/serp/google/organic/live/advanced", [{
        "keyword": keyword, "language_code": "en", "depth": 10, "load_async_ai_overview": True,
        "location_coordinate": f"{lat:.7f},{lng:.7f}",
    }], client)
    items = ((task.get("result") or [{}])[0] or {}).get("items") or []
    aio = next((i for i in items if i.get("type") == "ai_overview"), None)
    if not aio:
        return {"present": False, "text": "", "references": []}
    parts = [aio.get("text") or ""] + [(s.get("text") or "") for s in aio.get("items") or []]
    refs = [r.get("url") for r in aio.get("references") or [] if r.get("url")]
    return {"present": True, "text": "\n".join(p for p in parts if p), "references": refs}


def keyword_overview(keywords: list[str], location_code: int = 2840,
                     client: httpx.Client | None = None) -> list[dict]:
    """Search volume, CPC, competition, difficulty, intent per keyword (location 2840 = US)."""
    task = _post("/dataforseo_labs/google/keyword_overview/live", [{
        "keywords": keywords, "location_code": location_code, "language_code": "en",
    }], client)
    items = ((task.get("result") or [{}])[0] or {}).get("items") or []
    out = []
    for it in items:
        info = it.get("keyword_info") or {}
        props = it.get("keyword_properties") or {}
        intent = (it.get("search_intent_info") or {}).get("main_intent")
        out.append({"keyword": it.get("keyword"), "search_volume": info.get("search_volume"),
                    "cpc": info.get("cpc"), "competition": info.get("competition"),
                    "difficulty": props.get("keyword_difficulty"), "intent": intent})
    return out
