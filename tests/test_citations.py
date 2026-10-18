import json

from app import ai, brain, db, places_store
from app.modules.citations import engine

NAP = {"name": "Callahan & Sons Plumbing", "street": "2214 Lakewood Ave", "city": "Dallas", "state": "TX",
       "zip": "75214", "phone": "(214) 555-0142", "website": "https://callahanplumbing.example"}


def test_compare_fields():
    ok = engine.compare(NAP, {"name": "Callahan and Sons Plumbing", "address": "2214 Lakewood Avenue, Dallas, TX 75214",
                              "phone": "214-555-0142", "website": "http://www.callahanplumbing.example/", "hours": True})
    assert {k: v["status"] for k, v in ok.items() if k in ("name", "address", "phone", "website", "hours")} == \
        {"name": "match", "address": "match", "phone": "match", "website": "match", "hours": "present"}
    assert engine.listing_status(ok) == "live"
    bad = engine.compare(NAP, {"name": "Callahan Plumbing LLC", "address": "9 Elm St, Dallas, TX 75201", "phone": "214-555-9999"})
    assert bad["address"]["status"] == "mismatch" and bad["phone"]["found"] == "214-555-9999"
    assert bad["website"]["status"] == "absent" and engine.listing_status(bad) == "wrong"


def test_score_formula():
    rows = [{"status": "live"}] * 6 + [{"status": "wrong"}] * 4 + [{"status": "missing"}] * 2 + [{"status": "pending"}]
    assert engine.score(rows) == (67, "D")  # (6 + 0.5*4) / 12 = 66.7
    assert engine.score([{"status": "pending"}]) == (None, None)


def test_profile_from_address():
    p = engine.profile_from("X", "2214 Lakewood Ave, Dallas, TX 75214, USA", "1", "w")
    assert (p["street"], p["city"], p["state"], p["zip"]) == ("2214 Lakewood Ave", "Dallas", "TX", "75214")


def test_audit_run_with_claude_lookup(client, monkeypatch):
    monkeypatch.setattr(ai, "available", lambda: True)
    calls = []

    def fake(prompt, **kw):
        calls.append(prompt)
        assert kw.get("web") is True and "Never guess" in prompt
        dirs = [line[2:].split(" (")[0] for line in prompt.splitlines() if line.startswith("- ")]
        out = []
        for d in dirs:
            if d == "Yelp":
                out.append({"directory": d, "found": True, "url": "https://yelp.com/biz/c", "name": "Callahan & Sons Plumbing",
                            "address": "2214 Lakewood Ave, Dallas, TX 75214", "phone": "(214) 555-0000", "claimed": False})
            elif d == "Google Business Profile":
                out.append({"directory": d, "found": True, "name": "Callahan & Sons Plumbing", "address": "2214 Lakewood Ave, Dallas, TX 75214",
                            "phone": "(214) 555-0142", "website": "https://callahanplumbing.example"})
            else:
                out.append({"directory": d, "found": False})
        return out
    monkeypatch.setattr(ai, "ask_json", fake)
    client.post("/citations", data=NAP)
    a = db.one("SELECT * FROM citation_audits")
    assert a["status"] == "completed" and len(calls) == 4  # 16 general directories in batches of 5
    yelp = db.one("SELECT * FROM citation_listings WHERE directory = 'Yelp'")
    assert yelp["status"] == "wrong" and yelp["claimable"] == 1
    assert json.loads(yelp["fields"])["phone"]["found"] == "(214) 555-0000"
    page = client.get(f"/citations/{a['id']}")
    assert "unclaimed" in page.text and "shows (214) 555-0000" in page.text
    client.post(f"/citations/{a['id']}/share")
    tok = db.one("SELECT share_token FROM citation_audits")["share_token"]
    shared = client.get(f"/share/citations/{tok}")
    assert shared.status_code == 200 and "Edit" not in shared.text


def test_manual_audit_and_client_tracker(client):
    pid = places_store.upsert({"google_place_id": "ChIJy", "name": "Callahan & Sons Plumbing",
                               "address": "2214 Lakewood Ave, Dallas, TX 75214, USA", "phone": "(214) 555-0142"})
    cid = db.insert("contacts", place_id=pid, business_name="Callahan & Sons Plumbing", source="manual", status="new")
    from app.modules.clients.routes import convert
    co = convert(cid)
    assert engine.profile(co)["zip"] == "75214"
    client.post(f"/clients/{co}/citations/audit", data={"vertical": "home"})  # no claude in tests -> manual
    listings = db.all("SELECT * FROM citation_listings")
    assert any(l["directory"] == "Angi" for l in listings) and not any(l["directory"] == "Avvo" for l in listings)
    lid = next(l["id"] for l in listings if l["directory"] == "Facebook")
    client.post(f"/citations/listings/{lid}", data={"status": "pending", "name": "Callahan & Sons Plumbing",
                                                    "address": "2214 Lakewood Ave, Dallas TX 75214", "phone": "214.555.0142",
                                                    "submission_status": "live"})
    l = db.one("SELECT * FROM citation_listings WHERE id = ?", lid)
    assert l["status"] == "live" and l["submission_status"] == "live" and l["checked_by"] == "manual"
    assert client.get(f"/clients/{co}/citations").status_code == 200
    assert "Facebook" in client.get(f"/clients/{co}/citations/export.csv").text
    client.post(f"/clients/{co}/citations/to-brain")
    slug = db.one("SELECT vault_slug FROM companies WHERE id = ?", co)["vault_slug"]
    assert any("citations" in s for s in brain.vault_sources(slug))
    assert "75214" in client.get(f"/clients/{co}/citations/nap.csv").text
