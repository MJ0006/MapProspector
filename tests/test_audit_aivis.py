import json

import httpx

from app import ai, db, places_store
from app.modules.aivis import engine as aivis
from app.modules.audit import checks
from app.providers import ai_engines
from tests.conftest import FIXTURES

PLACE = {"name": "Ridgeline Roofing", "website": "https://ridgeline.example", "phone": "(303) 555-0100",
         "address": "100 Main St, Denver, CO 80202, USA", "rating": 4.6, "review_count": 87,
         "primary_category": "Roofing contractor", "photos_count": 3, "hours": ["Mon 8-5"], "business_status": "OPERATIONAL"}


def _site(html: str, status=200):
    return httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(status, text=html)),
                        follow_redirects=True)


def test_audit_good_homepage():
    url, res = checks.run(PLACE, client=_site((FIXTURES / "homepage.html").read_text()))
    by = {c["key"]: c for c in res}
    assert url == "https://ridgeline.example"
    for key in ("site_https", "title", "title_brand", "meta_description", "h1", "viewport", "schema_local",
                "nap_phone", "tel_link", "city_mention", "indexable", "canonical"):
        assert by[key]["status"] == "pass", (key, by[key])
    assert by["gbp_photos"]["status"] == "warn" and by["img_alt"]["status"] == "warn"
    assert "roofingcontractor" in by["schema_local"]["detail"]
    assert all(c["source"] for c in res)  # every finding cites a brain chapter


def test_audit_bad_homepage_and_no_website():
    html = '<html><head><meta name="robots" content="noindex"></head><body><p>hi</p></body></html>'
    _, res = checks.run(PLACE, client=_site(html))
    by = {c["key"]: c for c in res}
    for key in ("title", "meta_description", "h1", "viewport", "schema_local", "indexable", "nap_phone"):
        assert by[key]["status"] == "fail", key
    url, res = checks.run({**PLACE, "website": None})
    assert url is None and {c["key"]: c for c in res}["gbp_website"]["status"] == "fail"


def test_audit_route_and_outreach_draft(client, monkeypatch):
    pid = places_store.upsert({**PLACE, "website": None})
    cid = db.insert("contacts", place_id=pid, business_name="Ridgeline", email="o@r.ex", source="prospecting",
                    status="new", created_at=db.now())
    r = client.post("/audit/run", data={"place_id": pid}, follow_redirects=True)
    assert r.status_code == 200 and "Issues found" in r.text
    aid = db.one("SELECT id FROM audits")["id"]
    assert db.one("SELECT issues FROM audits")["issues"] >= 1
    seen = {}

    def fake(prompt, **kw):
        seen["prompt"] = prompt
        return {"subject": "Quick note about your Google listing", "body": "Hi, I noticed no website is linked."}
    monkeypatch.setattr(ai, "ask_json", fake)
    client.post(f"/audit/{aid}/draft-email")
    m = db.one("SELECT * FROM outbox")
    assert m["status"] == "draft" and m["contact_id"] == cid  # cold: needs approval
    assert "guarantee" in seen["prompt"].lower() and "Website linked on the listing" in seen["prompt"]
    client.post(f"/audit/{aid}/share")
    tok = db.one("SELECT share_token FROM audits")["share_token"]
    assert client.get(f"/share/audit/{tok}").status_code == 200


def test_tools_use_brain_prompt(client, monkeypatch):
    sent = {}
    monkeypatch.setattr(ai, "ask", lambda p, **k: sent.setdefault("p", p) and "DRAFT TITLE")
    assert client.get("/tools?prompt=prompt-generating-a-title-tag").status_code == 200
    r = client.post("/tools/run", data={"prompt": "prompt-generating-a-title-tag", "business_name": "Acme Plumbing",
                                        "city": "Austin"})
    assert "DRAFT TITLE" in r.text
    assert "Acme Plumbing" in sent["p"] and "Never stuff keywords" in sent["p"]


def test_parse_answer_and_presence():
    text = 'Here are top picks...\n1. Summit Roofing Group\n2. Ridgeline Roofing LLC\n{"businesses": [{"name": "Summit Roofing Group", "reason": "500 reviews"}, {"name": "Harbor Roofing", "reason": "fast"}, {"name": "Bay Roofers", "reason": ""}, {"name": "Ridgeline Roofing LLC", "reason": "local"}]}'
    found = aivis.parse_answer(text)
    assert [b["name"] for b in found][:2] == ["Summit Roofing Group", "Harbor Roofing"]
    assert aivis.presence("Ridgeline Roofing", found) == ("mentioned", 4)
    assert aivis.presence("Nobody Co", found) == ("absent", None)
    fallback = aivis.parse_answer("Options:\n1. **Alpha Plumbing** great\n2. Beta Rooter")
    assert [b["name"] for b in fallback] == ["Alpha Plumbing", "Beta Rooter"]


def test_aivis_run_end_to_end(client, monkeypatch):
    monkeypatch.setattr(ai_engines, "available", lambda: ["claude", "chatgpt"])

    def fake_ask(engine, q, client=None):
        if engine == "claude":
            biz = [{"name": "Ridgeline Roofing", "reason": "local"}, {"name": "Summit Roofing", "reason": "reviews"}]
            return {"text": "x\n" + json.dumps({"businesses": biz}), "sources": ["https://www.yelp.com/x", "https://ridgeline.example/"]}
        biz = [{"name": "Summit Roofing", "reason": "most reviews"}]
        return {"text": "y\n" + json.dumps({"businesses": biz}), "sources": ["https://www.yelp.com/y"]}
    monkeypatch.setattr(ai_engines, "ask", fake_ask)
    pid = places_store.upsert({"name": "Ridgeline Roofing", "website": "https://ridgeline.example"})
    r = client.post("/aivis", data={"business_name": "Ridgeline Roofing", "city": "Denver", "category": "roofer",
                                    "place_id": pid}, follow_redirects=True)
    assert r.status_code == 200
    rep = json.loads(db.one("SELECT report FROM aivis_runs")["report"])
    assert rep["answers"] == 10 and rep["recommended"] == 5 and rep["score"] == 50 and rep["grade"] == "C"
    claude = next(e for e in rep["engines"] if e["engine"] == "claude")
    assert claude["cited"] == 5 and claude["you_best"] == 1
    assert rep["competitors"][0]["name"] == "Summit Roofing"
    assert any(s["name"] == "yelp.com" and s["directory"] for s in rep["sources"])
    assert "Ridgeline Roofing" in r.text and "Engine by engine" in r.text
