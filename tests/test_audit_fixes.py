"""Regression tests for the pre-merge audit: security, stuck jobs, schedules, dates, compliance, journal."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app import ai, db, jobs, journal, mailer, notify, places_store, security
from app.modules.crm import automations, pipeline
from app.modules.rank import engine as rank


# --- security --------------------------------------------------------------------------------------

def test_cross_site_post_blocked_but_public_routes_open(client):
    cid = db.insert("contacts", business_name="X", source="manual", status="new")
    r = client.post(f"/crm/contacts/{cid}/delete", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and db.one("SELECT 1 FROM contacts WHERE id = ?", cid)
    r = client.post(f"/crm/contacts/{cid}/delete", headers={"Referer": "https://evil.example/page"})
    assert r.status_code == 403
    ok = client.post(f"/crm/contacts/{cid}/delete", headers={"Origin": "http://testserver"}, follow_redirects=False)
    assert ok.status_code == 303 and not db.one("SELECT 1 FROM contacts WHERE id = ?", cid)
    client.post("/crm/forms", data={"name": "W"})
    token = db.one("SELECT token FROM forms")["token"]
    r = client.post(f"/f/{token}", data={"name": "A B", "email": "a@b.co"}, headers={"Origin": "https://clientsite.example"},
                    follow_redirects=False)
    assert r.status_code == 303  # embedded website forms post cross-site on purpose


def test_remote_requests_need_password(client, monkeypatch):
    remote = {"X-Forwarded-For": "203.0.113.9"}
    assert client.get("/", headers=remote).status_code == 403
    assert client.get("/share/scan/x", headers=remote).status_code == 404  # public route reached (unknown token)
    monkeypatch.setenv("APP_PASSWORD", "s3cret")
    r = client.get("/crm/contacts", headers=remote, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert "Wrong password" in client.post("/login", data={"password": "nope", "next": "/"}).text
    r = client.post("/login", data={"password": "s3cret", "next": "//evil.example"}, follow_redirects=False)
    assert r.headers["location"] == "/"  # open redirect blocked
    assert client.get("/crm/contacts", headers=remote).status_code == 200  # cookie set by login


def test_safe_back_and_public_form_validation(client):
    assert security.safe_next("https://evil.example") == "/" and security.safe_next("//evil") == "/"
    assert security.safe_next("/crm/tasks?x=1") == "/crm/tasks?x=1"
    r = client.post("/crm/tasks", data={"title": "t", "back": "https://evil.example"}, follow_redirects=False)
    assert r.headers["location"] == "/crm/tasks"
    client.post("/crm/forms", data={"name": "W"})
    token = db.one("SELECT token FROM forms")["token"]
    assert client.post(f"/f/{token}", content=b"[1,2]", headers={"content-type": "application/json"}).status_code == 400


def test_heatmap_tooltip_escapes_names():
    js = open("app/static/heatmap.js").read()
    assert "p.top.map(esc)" in js and "function esc" in js


# --- jobs that used to get stuck -----------------------------------------------------------------

def test_crashed_scan_is_marked_failed(monkeypatch):
    pid = places_store.upsert({"name": "X", "lat": 1.0, "lng": 1.0})
    monkeypatch.setattr(rank.grid, "generate", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    [sid] = rank.create_scans(pid, ["x"], surface="google_maps", provider="demo", size=3, spacing_m=1000)
    s = db.one("SELECT * FROM scans WHERE id = ?", sid)
    assert s["status"] == "failed" and "boom" in s["error"]
    assert db.one("SELECT COUNT(*) n FROM events WHERE kind = 'failure'")["n"] >= 1  # journaled


def test_startup_recovery_requeues_interrupted_work():
    pid = places_store.upsert({"name": "X", "lat": 1.0, "lng": 1.0})
    sid = db.insert("scans", place_id=pid, keyword="x", surface="google_maps", provider="demo", grid_size=3,
                    spacing_m=1000, shape="square", center_lat=1.0, center_lng=1.0, status="in_progress")
    aid = db.insert("audits", place_id=pid, checks=[], issues=-1)
    db.insert("gbp_posts", company_id=db.insert("companies", name="C"), body="", status="drafting")
    jobs.recover()
    assert db.one("SELECT status FROM scans WHERE id = ?", sid)["status"] == "completed"
    assert db.one("SELECT issues FROM audits WHERE id = ?", aid)["issues"] >= 0
    assert db.one("SELECT status FROM gbp_posts")["status"] == "failed"


def test_scan_claim_prevents_double_runs():
    pid = places_store.upsert({"name": "X", "lat": 1.0, "lng": 1.0})
    sid = db.insert("scans", place_id=pid, keyword="x", surface="google_maps", provider="demo", grid_size=3,
                    spacing_m=1000, shape="square", center_lat=1.0, center_lng=1.0, status="queue")
    assert db.claim("scans", sid, ("queue",), "in_progress") is True
    assert db.claim("scans", sid, ("queue",), "in_progress") is False


def test_manual_citation_audit_does_not_spin(client):
    client.post("/citations", data={"name": "Acme", "phone": "5125550101"})  # no claude -> manual
    a = db.one("SELECT * FROM citation_audits")
    assert a["status"] == "manual"
    lid = db.one("SELECT id FROM citation_listings LIMIT 1")["id"]
    client.post(f"/citations/listings/{lid}", data={"status": "missing"})
    assert db.one("SELECT status FROM citation_audits")["status"] == "manual"
    assert "Checking directories" not in client.get(f"/citations/{a['id']}").text


def test_failing_schedule_advances_and_records_error(monkeypatch):
    pid = places_store.upsert({"name": "X", "lat": 1.0, "lng": 1.0})
    sched = db.insert("scan_schedules", name="s", place_id=pid, keywords=["x"], surface="google_maps", provider="places",
                      grid={"size": 3, "spacing_m": 1000}, recurrence="daily", next_run_at="2000-01-01T00:00:00+00:00")
    rank.run_due_schedules()  # places provider not configured -> fails
    s = db.one("SELECT * FROM scan_schedules WHERE id = ?", sched)
    assert s["next_run_at"] > datetime.now(timezone.utc).isoformat() and "not configured" in s["last_error"]


def test_unconfigured_provider_rejected():
    pid = places_store.upsert({"name": "X", "lat": 1.0, "lng": 1.0})
    with pytest.raises(ValueError, match="not configured"):
        rank.create_scans(pid, ["x"], surface="google_maps", provider="dataforseo", size=3, spacing_m=1000)


def test_chain_branch_not_matched_by_name():
    target = {"google_place_id": "branch-A", "name": "Joe's Plumbing"}
    results = [{"google_place_id": "branch-B", "name": "Joe's Plumbing"}, {"google_place_id": "branch-A", "name": "Joe's Plumbing"}]
    assert rank.find_rank(target, results) == 2
    assert rank.find_rank({"name": "Joe's Plumbing"}, results) == 1  # no id on target: name fallback still works


def test_background_drafting(client, monkeypatch):
    co = db.insert("companies", name="Acme", is_client=1)
    monkeypatch.setattr(ai, "available", lambda **k: True)
    monkeypatch.setattr(ai, "ask", lambda p, **k: "Post text")
    client.post(f"/clients/{co}/posts/draft", data={"topic": "t", "count": "3"})
    rows = db.all("SELECT * FROM gbp_posts")
    assert len(rows) == 3 and all(r["status"] == "draft" and r["body"] == "Post text" for r in rows)


def test_ai_timeout_is_handled(monkeypatch):
    import shutil
    import subprocess
    monkeypatch.setattr(shutil, "which", lambda n, *a, **k: "/usr/bin/claude")

    def slow(*a, **k):
        raise subprocess.TimeoutExpired("claude", 1)
    monkeypatch.setattr(subprocess, "run", slow)
    with pytest.raises(ai.AIUnavailable, match="did not answer"):
        ai.ask("hi", purpose="posts")
    assert db.one("SELECT outcome FROM events WHERE kind = 'ai_call'")["outcome"] == "failed"


# --- data correctness ------------------------------------------------------------------------------

def test_task_due_dates_are_utc_and_counted(client):
    local_soon = (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")
    client.post("/crm/tasks", data={"title": "Call back", "due_at": local_soon})
    due = db.one("SELECT due_at FROM tasks")["due_at"]
    assert due.endswith("+00:00")
    import re
    tile = re.search(r"<b>(\d+)</b><span>Tasks due within a day", client.get("/").text)
    assert tile and tile.group(1) == "1"


def test_move_deal_automation_uses_pipeline():
    stages = pipeline.pipelines_with_stages()[0]["stages"]
    won = next(s for s in stages if s["win_pct"] == 100)
    cid = db.insert("contacts", business_name="Acme", source="manual", status="new", tags=[])
    pipeline.create_deal(cid, "Retainer")
    aid = db.insert("automations", name="win", trigger="manual", actions=[{"type": "move_deal", "stage_id": won["id"]}], active=1)
    automations.start(aid, cid)
    assert db.one("SELECT status FROM deals")["status"] == "won"


def test_automation_recursion_is_bounded():
    stages = pipeline.pipelines_with_stages()[0]["stages"]
    s1, s2 = stages[1]["id"], stages[2]["id"]
    # Two automations that keep moving the deal back and forth.
    db.insert("automations", name="a", trigger="deal_stage", trigger_config={"stage_id": s1}, actions=[{"type": "move_deal", "stage_id": s2}], active=1)
    db.insert("automations", name="b", trigger="deal_stage", trigger_config={"stage_id": s2}, actions=[{"type": "move_deal", "stage_id": s1}], active=1)
    cid = db.insert("contacts", business_name="Acme", source="manual", status="new", tags=[])
    did = pipeline.create_deal(cid, "Loop")
    pipeline.move_deal(did, s1)  # must terminate
    assert db.one("SELECT COUNT(*) n FROM automation_runs")["n"] <= automations.MAX_DEPTH + 1


def test_old_database_gets_migrated(tmp_path):
    path = tmp_path / "old.db"
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE companies (id INTEGER PRIMARY KEY, name TEXT NOT NULL, place_id INTEGER, website TEXT, phone TEXT, "
              "address TEXT, timezone TEXT, is_client INTEGER NOT NULL DEFAULT 0, vault_slug TEXT, review_link TEXT, "
              "brand_voice TEXT, created_at TEXT)")
    c.commit()
    c.close()
    db.init(path)
    cols = {r["name"] for r in db.all("PRAGMA table_info(companies)")}
    assert {"gbp_location", "gsc_site", "lock_checked_at"} <= cols
    assert db.one("PRAGMA journal_mode")["journal_mode"] == "wal"


def test_places_photo_counts(monkeypatch):
    from app.providers import places
    full = places.normalize({"id": "x", "displayName": {"text": "A"}})          # photos requested, none returned
    scan = places.normalize({"id": "x", "displayName": {"text": "A"}}, places.SCAN_FIELDS)
    assert full["photos_count"] == 0 and scan["photos_count"] is None
    assert "photos" not in places.SCAN_FIELDS and "regularOpeningHours" not in places.SCAN_FIELDS


# --- compliance ------------------------------------------------------------------------------------

def test_cold_email_needs_postal_address_and_recipient():
    cid = db.insert("contacts", business_name="Peak", email="o@peak.example", source="prospecting", status="new")
    m = {"channel": "email", "to_addr": "o@peak.example", "contact_id": cid}
    assert "postal address" in mailer.approval_problem(m)
    db.set_setting("brand_address", "1 Main St")
    assert mailer.approval_problem(m) is None
    assert mailer.approval_problem({**m, "to_addr": ""}) == "Add a recipient first."
    assert "1 Main St" in mailer.with_current_footer("Hi\n--\nOld footer")


# --- journal + self-improvement --------------------------------------------------------------------

def test_decisions_feed_lessons_into_prompts(client, monkeypatch):
    co = db.insert("companies", name="Acme Plumbing", is_client=1)
    monkeypatch.setattr(ai, "available", lambda **k: True)
    monkeypatch.setattr(ai, "ask", lambda p, **k: "Thank you so much for your review!!! We loved it!")
    rid = db.insert("reviews", company_id=co, external_id="r1", rating=5, text="Great job", reply_status="none")
    client.post(f"/gbp/reviews/{rid}/draft")
    client.post(f"/gbp/reviews/{rid}/mark_replied", data={"text": "Thanks for the review. Glad we could help."})
    rid2 = db.insert("reviews", company_id=co, external_id="r2", rating=4, text="Good", reply_status="none")
    client.post(f"/gbp/reviews/{rid2}/draft")
    client.post(f"/gbp/reviews/{rid2}/mark_replied", data={"text": "Thanks, we appreciate it."})
    dec = db.all("SELECT * FROM events WHERE kind = 'decision' AND area = 'reviews'")
    assert len(dec) == 2 and all(d["outcome"] == "edited" for d in dec)
    assert json.loads(dec[0]["data"])["draft"].startswith("Thank you so much")

    monkeypatch.setattr(ai, "ask_json", lambda p, **k: {"lessons": [{"text": "No exclamation marks; keep replies to one or two calm sentences.", "evidence": [1, 2]}]})
    assert journal.propose_lessons("reviews", co) == 1
    lesson = db.one("SELECT * FROM lessons")
    assert lesson["status"] == "proposed"
    from app.modules.gbp import reviews
    assert "exclamation" not in reviews.reply_prompt(db.one("SELECT * FROM companies WHERE id = ?", co), {"rating": 5, "text": "x"})
    client.post(f"/journal/lessons/{lesson['id']}/approve", data={"text": lesson["text"]})
    prompt = reviews.reply_prompt(db.one("SELECT * FROM companies WHERE id = ?", co), {"rating": 5, "text": "x"})
    assert "No exclamation marks" in prompt  # approved lessons now steer future drafts
    page = client.get("/journal")
    assert page.status_code == 200 and "Review replies" in page.text and "edited" in page.text


def test_rejected_post_and_outbox_decisions(client, monkeypatch):
    co = db.insert("companies", name="Acme", is_client=1)
    monkeypatch.setattr(ai, "available", lambda **k: True)
    monkeypatch.setattr(ai, "ask", lambda p, **k: "AI post")
    client.post(f"/clients/{co}/posts/draft", data={"topic": "t"})
    pid = db.one("SELECT id FROM gbp_posts")["id"]
    client.post(f"/gbp/posts/{pid}/delete", data={"body": "AI post"})
    assert db.one("SELECT outcome FROM events WHERE kind = 'decision' AND area = 'posts'")["outcome"] == "rejected"


def test_notices_and_mcp_journal(client):
    co = db.insert("companies", name="Acme", is_client=1)
    db.insert("reviews", company_id=co, external_id="n1", rating=5, text="new", reply_status="none", created_at=db.now())
    db.insert("profile_changes", company_id=co, field="phone", status="open")
    journal.failure("rank", "scan", "quota exceeded")
    texts = [n["text"] for n in notify.notices()]
    assert any("new review" in t for t in texts) and any("Google listing" in t for t in texts) and any("failure" in t for t in texts)
    assert "notice-bar" in client.get("/").text
    client.post("/notices/seen")
    assert not any("new review" in n["text"] for n in notify.notices())
    from app import mcp_server
    out = mcp_server.get_journal(kind="failure")
    assert out["events"][0]["detail"] == "quota exceeded" and out["summary"]["failures"]
