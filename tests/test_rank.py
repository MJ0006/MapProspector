import json
from datetime import datetime, timezone

import httpx
import pytest

from app import db, places_store
from app.modules.rank import engine, grid
from app.providers import dataforseo, places
from tests.conftest import FIXTURES


def test_grid_sizes_and_shapes():
    assert len(grid.generate(39.7, -104.9, 7, 1609, "square")) == 49
    assert len(grid.generate(39.7, -104.9, 13, 1609, "square")) == 169
    assert len(grid.generate(39.7, -104.9, 9, 1609, "circle")) == 61  # LeadSnap: "a 9x9 circle about 61"
    pts = grid.generate(39.7, -104.9, 3, 1000)
    # row-major from north-west; centre point is the business location
    assert pts[0]["lat"] > pts[-1]["lat"] and pts[0]["lng"] < pts[-1]["lng"]
    assert pts[4]["lat"] == pytest.approx(39.7) and pts[4]["lng"] == pytest.approx(-104.9)
    # spacing really is ~1000 m
    assert grid.haversine_m(pts[3]["lat"], pts[3]["lng"], pts[4]["lat"], pts[4]["lng"]) == pytest.approx(1000, rel=0.01)
    with pytest.raises(ValueError):
        grid.generate(0, 0, 15, 1000)


def test_polygon_clip():
    poly = [[39.69, -104.95], [39.71, -104.95], [39.71, -104.85], [39.69, -104.85]]
    pts = grid.generate(39.7, -104.9, 7, 1000, polygon=poly)
    assert 0 < len(pts) < 49
    assert all(grid.point_in_polygon(p["lat"], p["lng"], poly) for p in pts)


def test_find_rank_matching():
    results = [{"google_place_id": "a", "name": "Alpha"}, {"google_place_id": "b", "name": "Bravo Roofing", "website": "https://bravo.com/x"}]
    assert engine.find_rank({"google_place_id": "b"}, results) == 2
    assert engine.find_rank({"name": "bravo roofing!"}, results) == 2
    assert engine.find_rank({"website": "http://www.bravo.com"}, results, "organic") == 2
    assert engine.find_rank({"google_place_id": "zz", "name": "Nope"}, results) is None


def test_demo_scan_end_to_end():
    pid = places_store.upsert({"name": "Ridgeline Roofing", "lat": 39.7392, "lng": -104.9903})
    [sid] = engine.create_scans(pid, ["roofing"], surface="google_maps", provider="demo", size=5, spacing_m=1000)
    scan = db.one("SELECT * FROM scans WHERE id = ?", sid)
    assert scan["status"] == "completed"
    stats = json.loads(scan["stats"])
    assert stats["points"] == 25 and stats["found_points"] == 25  # demo injects the target
    comps = engine.competitors(sid)
    you = [c for c in comps if c["you"]]
    assert len(you) == 1 and you[0]["avg_rank_all"] == stats["avg_rank_all"]
    # shares of top-3 slots across all businesses add up to 100 %
    assert sum(c["share_top3"] for c in comps) == pytest.approx(100, abs=1)
    assert set(you[0]["quadrants"]) == {"nw", "ne", "sw", "se"}


def test_failed_provider_marks_scan_failed(monkeypatch):
    pid = places_store.upsert({"name": "X", "lat": 1.0, "lng": 1.0})

    def boom(*a, **k):
        raise RuntimeError("quota exceeded")
    monkeypatch.setattr(engine, "provider_search", boom)
    [sid] = engine.create_scans(pid, ["x"], surface="google_maps", provider="demo", size=3, spacing_m=1000)
    scan = db.one("SELECT * FROM scans WHERE id = ?", sid)
    assert scan["status"] == "failed" and "quota exceeded" in scan["error"]


def test_places_mode_rejects_unsupported_surface():
    pid = places_store.upsert({"name": "X", "lat": 1.0, "lng": 1.0})
    with pytest.raises(ValueError):
        engine.create_scans(pid, ["x"], surface="organic", provider="places", size=3, spacing_m=1000)


def _mock(fixture: str, seen: list):
    def handler(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200, json=json.loads((FIXTURES / fixture).read_text()))
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_places_text_search_parses_and_biases(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "test")
    seen = []
    res = places.text_search("roofing", 39.7, -104.9, radius_m=1500, client=_mock("places_textsearch.json", seen))
    assert res[0]["name"] == "Ridgeline Roofing" and res[0]["review_count"] == 87 and res[0]["photos_count"] == 2
    assert res[0]["categories"] == ["roofing_contractor"]
    body = json.loads(seen[0].content)
    assert body["locationBias"]["circle"]["radius"] == 1500.0
    assert seen[0].headers["X-Goog-Api-Key"] == "test"
    assert "places.rating" in seen[0].headers["X-Goog-FieldMask"]


def test_dataforseo_maps_and_keywords(monkeypatch):
    monkeypatch.setenv("DATAFORSEO_LOGIN", "u")
    monkeypatch.setenv("DATAFORSEO_PASSWORD", "p")
    seen = []
    res = dataforseo.maps_search("roofing", 39.7, -104.9, 1000, client=_mock("dataforseo_maps.json", seen))
    assert [r["name"] for r in res] == ["Summit Roofing Group", "Ridgeline Roofing"]  # sorted by rank_group
    assert json.loads(seen[0].content)[0]["location_coordinate"].endswith(",14z")
    kw = dataforseo.keyword_overview(["roofing"], client=_mock("dataforseo_keywords.json", []))
    assert kw[0] == {"keyword": "roofing", "search_volume": 165000, "cpc": 12.5, "competition": 0.4,
                     "difficulty": 38, "intent": "commercial"}


def test_next_run_recurrences():
    t = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)  # a Sunday
    assert engine.next_run("daily", None, t).date().isoformat() == "2026-10-19"
    assert engine.next_run("weekly", 0, t).date().isoformat() == "2026-10-19"   # next Monday
    assert engine.next_run("monthly_day", 31, t).date().isoformat() == "2026-10-31"
    assert engine.next_run("first_monday", None, t).date().isoformat() == "2026-11-02"


def test_schedule_runs_and_pages_render(client):
    r = client.post("/rank/new", data={"manual_name": "Ridgeline Roofing", "manual_lat": "39.7392",
                                       "manual_lng": "-104.9903", "keywords": "roofing, roof repair",
                                       "provider": "demo", "surface": "google_maps", "size": "3",
                                       "spacing": "1", "unit": "mi", "shape": "square",
                                       "recurrence": "weekly", "day": "0"}, follow_redirects=False)
    assert r.status_code == 303
    assert db.one("SELECT COUNT(*) n FROM scans")["n"] == 2
    sched = db.one("SELECT * FROM scan_schedules")
    assert sched["next_run_at"] and sched["last_run_at"]
    scan = db.one("SELECT * FROM scans ORDER BY id LIMIT 1")
    for url in ("/rank", f"/rank/{scan['id']}", f"/rank/{scan['id']}/data", "/rank/new",
                "/api/grid-preview?lat=1&lng=1&size=5"):
        assert client.get(url).status_code == 200, url
    client.post(f"/rank/{scan['id']}/share")
    token = db.one("SELECT share_token FROM scans WHERE id = ?", scan["id"])["share_token"]
    page = client.get(f"/share/scan/{token}")
    assert page.status_code == 200 and "Flip the map" in page.text and "Settings" not in page.text
    assert client.get("/share/scan/nope").status_code == 404


def test_lead_search_demo_and_save(client):
    page = client.get("/leads/search?niche=plumber&lat=30.27&lng=-97.74")
    assert page.status_code == 200 and "(demo)" in page.text
    r = client.post("/leads/save-place", data={"name": "Peak Plumbing", "lat": "30.27", "lng": "-97.74"},
                    headers={"HX-Request": "true"})
    assert "saved" in r.text
    assert db.one("SELECT COUNT(*) n FROM contacts")["n"] == 1
