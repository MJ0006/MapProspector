"""Geo-grid generation (A1): centre + size + spacing in metres, square / circle / polygon clip.

Points are ordered row by row from the north-west corner, matching how heat maps are read.
"""
import math

M_PER_DEG_LAT = 111_320.0
MILE_M = 1609.344


def offset(lat: float, lng: float, north_m: float, east_m: float) -> tuple[float, float]:
    dlat = north_m / M_PER_DEG_LAT
    dlng = east_m / (M_PER_DEG_LAT * math.cos(math.radians(lat)))
    return lat + dlat, lng + dlng


def point_in_polygon(lat: float, lng: float, polygon: list[list[float]]) -> bool:
    """Ray casting; polygon is [[lat, lng], ...]."""
    inside = False
    n = len(polygon)
    for i in range(n):
        y1, x1 = polygon[i]
        y2, x2 = polygon[(i + 1) % n]
        if (y1 > lat) != (y2 > lat):
            x_cross = x1 + (lat - y1) * (x2 - x1) / (y2 - y1)
            if lng < x_cross:
                inside = not inside
    return inside


def generate(lat: float, lng: float, size: int, spacing_m: float, shape: str = "square",
             polygon: list[list[float]] | None = None) -> list[dict]:
    """Return [{idx, lat, lng, row, col}] for a size×size grid (3–13 per side)."""
    if not 1 <= size <= 13:
        raise ValueError("Grid size must be between 1 and 13 points per side.")
    if spacing_m <= 0:
        raise ValueError("Spacing must be positive.")
    half = (size - 1) / 2
    # A disc of radius (half + 0.25) steps keeps the round look (9×9 -> 61 points).
    r2 = (half + 0.25) ** 2
    points = []
    for row in range(size):
        for col in range(size):
            dy, dx = half - row, col - half
            if shape == "circle" and dy * dy + dx * dx > r2:
                continue
            plat, plng = offset(lat, lng, dy * spacing_m, dx * spacing_m)
            if polygon and len(polygon) >= 3 and not point_in_polygon(plat, plng, polygon):
                continue
            points.append({"lat": round(plat, 7), "lng": round(plng, 7), "row": row, "col": col})
    for i, p in enumerate(points):
        p["idx"] = i
    return points


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))
