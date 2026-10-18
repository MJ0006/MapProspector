"""Shared storage for businesses (the `places` table).

google_place_id is kept as the durable key; other Places fields are a cache with
fetched_at so they can be refreshed (Google's Places terms limit long-term caching).
"""
import json

from app import db


def upsert(p: dict) -> int:
    """Insert or refresh a normalised business dict; returns places.id."""
    cols = {
        "google_place_id": p.get("google_place_id"),
        "cid": p.get("cid"),
        "name": p.get("name") or "Unknown",
        "address": p.get("address"), "phone": p.get("phone"), "website": p.get("website"),
        "lat": p.get("lat"), "lng": p.get("lng"),
        "rating": p.get("rating"), "review_count": p.get("review_count"),
        "primary_category": p.get("primary_category"),
        "categories": json.dumps(p.get("categories") or []),
        "photos_count": p.get("photos_count"),
        "hours": json.dumps(p.get("hours")) if p.get("hours") else None,
        "business_status": p.get("business_status"),
        "maps_url": p.get("maps_url"),
        "fetched_at": db.now(),
    }
    existing = db.one("SELECT id FROM places WHERE google_place_id = ?", cols["google_place_id"]) \
        if cols["google_place_id"] else None
    if existing:
        # Keep values we already have when a provider omits a field.
        db.update("places", existing["id"], **{k: v for k, v in cols.items() if v not in (None, "[]")})
        return existing["id"]
    return db.insert("places", **cols)


def get(place_id: int) -> dict | None:
    p = db.one("SELECT * FROM places WHERE id = ?", place_id)
    if p:
        p["categories"] = db.loads(p["categories"], [])
        p["hours"] = db.loads(p["hours"], None)
    return p


def as_target(p: dict) -> dict:
    """The fields rank matching needs."""
    return {k: p.get(k) for k in ("google_place_id", "cid", "name", "website", "lat", "lng", "address",
                                  "rating", "review_count", "primary_category", "photos_count")}
