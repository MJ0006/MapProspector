import io
import json
import time

import httpx
from PIL import Image

from app import ai, brain, db, places_store
from app.modules.gbp import lock, reviews
from app.modules.gbp.routes import perf_summary
from app.providers import gbp_api


def _client_company(client, **place):
    pid = places_store.upsert({"google_place_id": "ChIJx", "name": "Acme Plumbing", "lat": 30.27, "lng": -97.74,
                               "phone": "(512) 555-0101", "website": "https://acme.example", **place})
    cid = db.insert("contacts", place_id=pid, business_name="Acme Plumbing", source="prospecting", status="new",
                    created_at=db.now())
    r = client.post("/clients/convert", data={"contact_id": cid}, follow_redirects=False)
    return int(r.headers["location"].rsplit("/", 1)[1]), cid, pid


def test_convert_creates_vault_and_plan(client):
    co_id, cid, _ = _client_company(client)
    co = db.one("SELECT * FROM companies WHERE id = ?", co_id)
    assert co["is_client"] == 1 and co["vault_slug"] == "acme-plumbing"
    assert "placeid=ChIJx" in co["review_link"]
    assert brain.vault_path("acme-plumbing").exists()
    assert db.one("SELECT status FROM contacts WHERE id = ?", cid)["status"] == "client"
    fx = brain.settings.BRAIN_DIR / "tests" / "fixtures"
    for kind, name in [("gbp", "sample-gbp.csv"), ("reviews", "sample-reviews.json")]:
        r = client.post(f"/clients/{co_id}/evidence", data={"kind": kind},
                        files={"file": (name, (fx / name).read_bytes())})
        assert r.status_code == 200, r.text
    assert db.one("SELECT COUNT(*) n FROM reviews WHERE company_id = ?", co_id)["n"] > 0  # reviews also imported
    client.post(f"/clients/{co_id}/synthesize")
    page = client.get(f"/clients/{co_id}")
    assert "Health Scorecard" in page.text and "Draft" in page.text
    assert client.get(f"/clients/{co_id}/weekly-report").status_code == 200
    # converting twice returns the same client
    assert client.post("/clients/convert", data={"contact_id": cid}, follow_redirects=False).headers["location"].endswith(f"/{co_id}")


def test_review_reply_flow(client, monkeypatch):
    co_id, _, _ = _client_company(client)
    path = reviews.settings.UPLOAD_DIR / "r.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("rating,date,text,author\n2,2026-10-01,Came late and left a mess,Jane Doe\n")
    assert reviews.import_file(co_id, path) == 1
    assert reviews.import_file(co_id, path) == 0  # de-duplicated
    rv = db.one("SELECT * FROM reviews")
    assert rv["author_hash"] and "Jane" not in json.dumps(rv)
    seen = {}
    monkeypatch.setattr(ai, "ask", lambda p, **k: seen.setdefault("p", p) and "We're sorry about the delay.")
    client.post(f"/gbp/reviews/{rv['id']}/draft")
    assert db.one("SELECT reply_status FROM reviews")["reply_status"] == "draft"
    assert "Do not offer incentives" in seen["p"] and "Jane" not in seen["p"]
    client.post(f"/gbp/reviews/{rv['id']}/approve", data={"text": "We're sorry, please call us."})
    assert db.one("SELECT reply_status FROM reviews")["reply_status"] == "approved"  # not connected: you paste it
    client.post(f"/gbp/reviews/{rv['id']}/mark_replied", data={"text": "We're sorry, please call us."})
    assert db.one("SELECT reply_status FROM reviews")["reply_status"] == "published"


def test_review_request_has_no_gating(client):
    co_id, _, _ = _client_company(client)
    cust = db.insert("contacts", company_id=co_id, first_name="Sam", email="sam@x.com", source="manual", status="client")
    client.post(f"/clients/{co_id}/review-requests", data={"contact_id": cust})
    rr = db.one("SELECT * FROM review_requests")
    assert "/r/" + rr["token"] in db.one("SELECT body FROM outbox")["body"]
    page = client.get(f"/r/{rr['token']}")
    assert "Leave a Google review" in page.text  # always offered, regardless of sentiment
    go = client.get(f"/r/{rr['token']}/go", follow_redirects=False)
    assert go.status_code == 302 and "writereview" in go.headers["location"]
    client.post(f"/r/{rr['token']}/feedback", data={"message": "Great but pricey"})
    rr = db.one("SELECT * FROM review_requests")
    assert rr["opened_at"] and rr["clicked_at"] and db.one("SELECT message FROM feedback")["message"]


def test_post_draft_and_reminder(client, monkeypatch):
    co_id, _, _ = _client_company(client)
    monkeypatch.setattr(ai, "ask", lambda p, **k: "Fall is here: book a water heater check.")
    client.post(f"/clients/{co_id}/posts/draft", data={"topic": "Water heater check", "count": "2"})
    rows = db.all("SELECT * FROM gbp_posts")
    assert len(rows) == 2 and rows[0]["status"] == "draft"
    client.post(f"/gbp/posts/{rows[0]['id']}/schedule", data={"body": rows[0]["body"], "scheduled_at": "2000-01-01T09:00"})
    from app.modules.gbp import posts
    posts.publish_scheduled()
    p = db.one("SELECT * FROM gbp_posts WHERE id = ?", rows[0]["id"])
    assert p["status"] == "approved" and "post it in Google" in p["error"]


def test_photo_upload_rename_and_gps(client):
    co_id, _, _ = _client_company(client)
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), "blue").save(buf, "JPEG")
    client.post(f"/clients/{co_id}/photos", data={"caption": "new water heater install", "category": "AT_WORK",
                                                  "stamp_gps": "1", "lat": "30.27", "lng": "-97.74"},
                files=[("files", ("IMG_0001.JPG", buf.getvalue(), "image/jpeg"))])
    ph = db.one("SELECT * FROM photos")
    assert ph["filename"] == "acme-plumbing-new-water-heater-install-1.jpg" and ph["gps_lat"] == 30.27
    with Image.open(reviews.settings.UPLOAD_DIR / "photos" / str(co_id) / ph["filename"]) as im:
        assert im.getexif().get_ifd(0x8825)[1] == "N"
    assert client.get(f"/media/photos/{co_id}/{ph['filename']}").status_code == 200
    client.post(f"/clients/{co_id}/photos/schedule", data={"photo_id": ph["id"], "start": "2026-11-01T09:00", "every_days": "7"})
    assert db.one("SELECT status FROM photos")["status"] == "scheduled"


def test_profile_lock_detects_changes(client, monkeypatch):
    co_id, _, _ = _client_company(client)
    base = {"name": "Acme Plumbing", "phone": "(512) 555-0101", "website": "https://acme.example",
            "address": "1 Main", "primary_category": "Plumber", "hours": ["Mon 8-5"], "business_status": "OPERATIONAL"}
    lock.approve(co_id, base)
    assert lock.check(co_id, {**base, "phone": "(512) 555-9999", "business_status": "CLOSED_TEMPORARILY"}) == 2
    assert lock.check(co_id, {**base, "phone": "(512) 555-9999", "business_status": "CLOSED_TEMPORARILY"}) == 2  # no dupes
    ch = db.one("SELECT * FROM profile_changes WHERE field = 'phone'")
    assert ch["live_value"] == "(512) 555-9999"
    client.post(f"/gbp/changes/{ch['id']}/revert")
    assert db.one("SELECT status FROM profile_changes WHERE id = ?", ch["id"])["status"] == "open"  # not connected: manual
    client.post(f"/gbp/changes/{ch['id']}/fixed")
    assert db.one("SELECT status FROM profile_changes WHERE id = ?", ch["id"])["status"] == "reverted"
    assert client.get(f"/clients/{co_id}/lock").status_code == 200


def test_perf_summary_and_api_parsing(monkeypatch):
    from datetime import date
    series = {"CALL_CLICKS": {"2026-10-01": 5, "2026-10-02": 5, "2026-09-30": 4},
              "BUSINESS_IMPRESSIONS_MOBILE_MAPS": {"2026-10-01": 100}}
    s = perf_summary(series, date(2026, 10, 1), date(2026, 10, 2))
    assert s["Calls"] == {"current": 10, "prior": 4, "change_pct": 150.0}
    assert s["Appearances (Maps)"]["change_pct"] is None  # no prior baseline
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "id")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "s")
    gbp_api._save({"access_token": "tok", "refresh_token": "r", "expires_in": 3600})
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"multiDailyMetricTimeSeries": [{"dailyMetricTimeSeries": [
            {"dailyMetric": "CALL_CLICKS", "timeSeries": {"datedValues": [{"date": {"year": 2026, "month": 10, "day": 1}, "value": "7"}]}}]}]})
    out = gbp_api.performance("accounts/1/locations/2", "2026-10-01", "2026-10-02", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert out == {"CALL_CLICKS": {"2026-10-01": 7}}
    assert "locations/2:fetchMultiDailyMetricsTimeSeries" in str(seen[0].url) and seen[0].headers["Authorization"] == "Bearer tok"


def test_client_pages_render(client):
    co_id, _, _ = _client_company(client)
    for tab in ("", "/posts", "/reviews", "/photos", "/lock", "/insights", "/optimize"):
        assert client.get(f"/clients/{co_id}{tab}").status_code == 200, tab
    assert client.get("/clients").status_code == 200
