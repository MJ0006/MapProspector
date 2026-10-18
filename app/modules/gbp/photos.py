"""Photo library and scheduling (B4).

GPS EXIF stamping is offered only as metadata: local-seo-brain rates "geotagging boosts rankings"
as unsupported folklore (claim exif-ranking-unsupported), and Google strips most EXIF anyway.
Publishing through the API needs a publicly reachable URL for the image (PUBLIC_BASE_URL).
"""
import re
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path

from app import db, jobs, settings
from app.providers import gbp_api

CATEGORIES = ["ADDITIONAL", "EXTERIOR", "INTERIOR", "PRODUCT", "AT_WORK", "TEAMS", "COVER", "LOGO"]
ALLOWED = {".jpg", ".jpeg", ".png", ".webp"}


def photo_dir(company_id: int) -> Path:
    d = settings.UPLOAD_DIR / "photos" / str(company_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def descriptive_name(business: str, caption: str, original: str, n: int) -> str:
    base = "-".join(w for w in re.sub(r"[^a-z0-9 ]", " ", f"{business} {caption}".lower()).split() if w)[:70]
    ext = Path(original).suffix.lower() if Path(original).suffix.lower() in ALLOWED else ".jpg"
    return f"{base or 'photo'}-{n}{ext}"


def _dms(value: float):
    value = abs(value)
    d = int(value)
    m = int((value - d) * 60)
    s = round((value - d - m / 60) * 3600, 2)
    return tuple(Fraction(x).limit_denominator(1000) for x in (d, m, s))


def stamp_gps(path: Path, lat: float, lng: float) -> bool:
    """Write GPS EXIF into a JPEG. Metadata only, with no ranking effect. Returns False if not possible."""
    try:
        from PIL import Image
    except ImportError:
        return False
    if path.suffix.lower() not in (".jpg", ".jpeg"):
        return False
    with Image.open(path) as im:
        exif = im.getexif()
        gps = {1: "N" if lat >= 0 else "S", 2: _dms(lat), 3: "E" if lng >= 0 else "W", 4: _dms(lng)}
        exif[0x8825] = gps
        im.save(path, exif=exif.tobytes())
    return True


def schedule_drip(company_id: int, photo_ids: list[int], start: datetime, every_days: int) -> None:
    for i, pid in enumerate(photo_ids):
        when = start + timedelta(days=every_days * i)
        db.update("photos", pid, status="scheduled", scheduled_at=when.isoformat(timespec="seconds"))


def publish(photo_id: int) -> str:
    p = db.one("SELECT * FROM photos WHERE id = ?", photo_id)
    co = db.one("SELECT * FROM companies WHERE id = ?", p["company_id"])
    if not (gbp_api.connected() and co["gbp_location"]):
        return "manual"
    url = f"{settings.PUBLIC_BASE_URL}/media/photos/{p['company_id']}/{p['filename']}"
    gbp_api.upload_photo(co["gbp_location"], url, p["category"] or "ADDITIONAL")
    db.update("photos", photo_id, status="published", published_at=db.now())
    return "published"


@jobs.on_tick
def publish_due() -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for p in db.all("SELECT id FROM photos WHERE status = 'scheduled' AND scheduled_at <= ?", now):
        try:
            if publish(p["id"]) == "manual":
                db.update("photos", p["id"], status="library", scheduled_at=None)
                co = db.one("SELECT company_id FROM photos WHERE id = ?", p["id"])
                db.insert("tasks", company_id=co["company_id"], title=f"Upload scheduled photo #{p['id']} to Google",
                          due_at=now, created_at=db.now())
        except gbp_api.GBPError as exc:
            db.update("photos", p["id"], status="failed")
