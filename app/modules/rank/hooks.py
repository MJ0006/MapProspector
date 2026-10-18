"""What happens after a scan completes. Client scans are exported to the client's brain vault
as geo-grid CSV (the format vendor/local-seo-brain/scripts/ingest_geogrid.py reads)."""
import csv

from app import brain, db, settings


def scan_completed(scan_id: int) -> None:
    scan = db.one("SELECT * FROM scans WHERE id = ?", scan_id)
    company = db.one("SELECT * FROM companies WHERE place_id = ? AND is_client = 1 AND vault_slug IS NOT NULL",
                     scan["place_id"]) if scan["place_id"] else None
    if not company:
        return
    path = settings.UPLOAD_DIR / f"geogrid-scan-{scan_id}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["keyword", "rank", "lat", "lng"])
        for p in db.all("SELECT * FROM scan_points WHERE scan_id = ? AND results IS NOT NULL ORDER BY idx", scan_id):
            w.writerow([scan["keyword"], p["rank"] if p["rank"] else "", p["lat"], p["lng"]])
    brain.ingest("geogrid", company["vault_slug"], path)
