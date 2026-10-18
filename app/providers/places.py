"""Google Places API (New): text search, place details, geocoding a city.

Docs: https://developers.google.com/maps/documentation/places/web-service/text-search
Results are normalised to one dict shape used everywhere in the app (see `normalize`).
"""
import httpx

from app import settings

BASE = "https://places.googleapis.com/v1"
FIELDS = [
    "id", "displayName", "formattedAddress", "location", "rating", "userRatingCount",
    "primaryTypeDisplayName", "types", "websiteUri", "nationalPhoneNumber", "googleMapsUri",
    "businessStatus", "photos", "regularOpeningHours",
]
# Per-point rank-scan searches only need what the heat map and competitor table show. Google bills a
# Text Search request at the tier of the most expensive field asked for, so leaving out photos, hours,
# phone and website keeps big grids cheaper. PLACES_SCAN_DETAIL=lite also drops rating/review counts.
SCAN_FIELDS = ["id", "displayName", "formattedAddress", "location", "primaryTypeDisplayName", "types",
               "googleMapsUri", "rating", "userRatingCount"]
if settings.env("PLACES_SCAN_DETAIL") == "lite":
    SCAN_FIELDS = SCAN_FIELDS[:-2]


class PlacesError(RuntimeError):
    pass


def _key() -> str:
    key = settings.env("GOOGLE_API_KEY")
    if not key:
        raise PlacesError("Set GOOGLE_API_KEY in .env to use Google Places.")
    return key


def _headers(fields: list[str], prefix: str = "places.") -> dict:
    mask = ",".join(prefix + f for f in fields)
    if prefix:
        mask += ",nextPageToken"
    return {"X-Goog-Api-Key": _key(), "X-Goog-FieldMask": mask, "Content-Type": "application/json"}


def normalize(p: dict, fields: list[str] | None = None) -> dict:
    loc = p.get("location") or {}
    hours = (p.get("regularOpeningHours") or {}).get("weekdayDescriptions")
    return {
        "google_place_id": p.get("id"),
        "name": (p.get("displayName") or {}).get("text", ""),
        "address": p.get("formattedAddress"),
        "lat": loc.get("latitude"),
        "lng": loc.get("longitude"),
        "rating": p.get("rating"),
        "review_count": p.get("userRatingCount"),
        "primary_category": (p.get("primaryTypeDisplayName") or {}).get("text"),
        "categories": [t for t in p.get("types", []) if t not in ("point_of_interest", "establishment")],
        "website": p.get("websiteUri"),
        "phone": p.get("nationalPhoneNumber"),
        "maps_url": p.get("googleMapsUri"),
        "business_status": p.get("businessStatus"),
        # Google omits empty fields and returns at most 10 photos: 10 means "10 or more",
        # None means photos were not requested (rank-scan searches).
        "photos_count": len(p.get("photos") or []) if "photos" in (fields or FIELDS) else None,
        "hours": hours,
    }


def text_search(query: str, lat: float | None = None, lng: float | None = None,
                radius_m: float | None = None, max_results: int = 20,
                client: httpx.Client | None = None, fields: list[str] | None = None) -> list[dict]:
    """Ranked results for a query, optionally biased to a circle around (lat, lng)."""
    body: dict = {"textQuery": query, "pageSize": min(20, max_results)}
    if lat is not None and lng is not None:
        body["locationBias"] = {"circle": {"center": {"latitude": lat, "longitude": lng},
                                           "radius": float(min(max(radius_m or 1000, 100), 50000))}}
    out: list[dict] = []
    http = client or httpx.Client(timeout=30)
    try:
        while len(out) < max_results:
            try:
                r = http.post(f"{BASE}/places:searchText", json=body, headers=_headers(fields or FIELDS))
            except httpx.HTTPError as exc:
                raise PlacesError(f"Could not reach Google Places: {exc}") from exc
            if r.status_code != 200:
                raise PlacesError(f"Places search failed ({r.status_code}): {r.text[:300]}")
            data = r.json()
            out += [normalize(p, fields) for p in data.get("places", [])]
            token = data.get("nextPageToken")
            if not token:
                break
            body = {**body, "pageToken": token}
    finally:
        if client is None:
            http.close()
    return out[:max_results]


def details(google_place_id: str, client: httpx.Client | None = None) -> dict:
    http = client or httpx.Client(timeout=30)
    try:
        r = http.get(f"{BASE}/places/{google_place_id}", headers=_headers(FIELDS, prefix=""))
    except httpx.HTTPError as exc:
        raise PlacesError(f"Could not reach Google Places: {exc}") from exc
    finally:
        if client is None:
            http.close()
    if r.status_code != 200:
        raise PlacesError(f"Place details failed ({r.status_code}): {r.text[:300]}")
    return normalize(r.json())


def geocode(text: str, client: httpx.Client | None = None) -> tuple[float, float] | None:
    """Rough centre for a city/area name via text search."""
    results = text_search(text, max_results=1, client=client)
    if results and results[0]["lat"] is not None:
        return results[0]["lat"], results[0]["lng"]
    return None
