"""Demo rank provider: deterministic FAKE businesses so the app can be tried without keys.

Every result is clearly fictional and the UI labels scans from this provider "Demo data".
"""
import hashlib
import math
import random

from app.modules.rank.grid import haversine_m, offset

PREFIXES = ["Summit", "Harbor", "Bayview", "Ridgeline", "Stonegate", "Northside", "Evergreen",
            "Cornerstone", "Liberty", "Pioneer", "Granite", "Maple", "Riverbend", "Oakwood",
            "Lakeside", "Metro", "Keystone", "Bluebird", "Ironwood", "Sunrise", "Westfield",
            "Copperline", "Trailhead", "Brightway", "Hometown"]


def _rng(*parts) -> random.Random:
    seed = hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()
    return random.Random(int(seed[:16], 16))


def businesses(keyword: str, lat: float, lng: float, n: int = 25) -> list[dict]:
    trade = keyword.replace(" near me", "").strip().title() or "Services"
    rng = _rng("biz", keyword.lower(), round(lat, 2), round(lng, 2))
    out = []
    for i, prefix in enumerate(PREFIXES[:n]):
        blat, blng = offset(lat, lng, rng.uniform(-6000, 6000), rng.uniform(-6000, 6000))
        out.append({
            "google_place_id": f"demo-{hashlib.md5(f'{keyword}{i}'.encode()).hexdigest()[:12]}",
            "name": f"{prefix} {trade} (demo)",
            "address": f"{100 + i * 17} Demo St",
            "lat": blat, "lng": blng,
            "rating": round(rng.uniform(3.6, 5.0), 1),
            "review_count": rng.randint(4, 400),
            "primary_category": trade,
            "categories": [],
            "website": f"https://example.com/{prefix.lower()}" if rng.random() > 0.2 else None,
            "phone": None, "maps_url": None,
            "photos_count": rng.randint(0, 120),
            "_strength": rng.uniform(0.6, 1.6),
        })
    return out


def search(keyword: str, lat: float, lng: float, center: tuple[float, float],
           extra: dict | None = None, depth: int = 20) -> list[dict]:
    """Rank fake businesses by strength / distance from the searcher's point."""
    pool = businesses(keyword, *center)
    if extra and not any(b["google_place_id"] == extra.get("google_place_id") for b in pool):
        pool.append({**extra, "_strength": 1.0})
    jitter = _rng("pt", keyword, round(lat, 5), round(lng, 5))
    scored = []
    for b in pool:
        d = haversine_m(lat, lng, b["lat"], b["lng"]) if b.get("lat") is not None else 3000
        score = b["_strength"] / math.log(d + 400) * jitter.uniform(0.85, 1.15)
        scored.append((score, b))
    scored.sort(key=lambda t: -t[0])
    return [{k: v for k, v in b.items() if not k.startswith("_")} for _, b in scored[:depth]]
