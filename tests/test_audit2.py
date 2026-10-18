"""Regression tests for the second full audit (one test per fixed defect)."""
import io
import json

from PIL import Image

from app import ai, brain, db, security
from app.modules.crm.questionnaires import link
from tests.test_clients_gbp import _client_company


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (20, 20), "red").save(buf, "JPEG")
    return buf.getvalue()


# --- A. wrong results / lost data ------------------------------------------------------------------

def test_json_list_of_objects_is_returned_whole():
    assert ai.extract_json('[{"directory": "Yelp"}, {"directory": "BBB"}]') == [{"directory": "Yelp"}, {"directory": "BBB"}]
    assert ai.extract_json('Checked {business name} for you:\n[{"a": 1}]') == [{"a": 1}]
    assert ai.extract_json('```json\n{"lessons": []}\n```') == {"lessons": []}


def test_photo_upload_after_delete_never_overwrites(client):
    co_id, _, _ = _client_company(client)
    up = lambda: client.post(f"/clients/{co_id}/photos", data={"caption": ""},
                             files=[("files", ("a.jpg", _jpeg(), "image/jpeg"))])
    for _ in range(3):
        up()
    first = db.one("SELECT * FROM photos ORDER BY id LIMIT 1")
    client.post(f"/gbp/photos/{first['id']}/delete")
    up()
    names = [r["filename"] for r in db.all("SELECT filename FROM photos")]
    assert len(names) == len(set(names)) == 3


def test_photo_upload_with_bad_gps_is_rejected_before_saving(client):
    co_id, _, _ = _client_company(client)
    r = client.post(f"/clients/{co_id}/photos", data={"stamp_gps": "1", "lat": "40.1N", "lng": "-97"},
                    files=[("files", ("a.jpg", _jpeg(), "image/jpeg"))])
    assert r.status_code == 400 and db.one("SELECT COUNT(*) n FROM photos")["n"] == 0
    from app.modules.gbp import photos
    assert not list(photos.photo_dir(co_id).glob("*.jpg"))
    r = client.post(f"/clients/{co_id}/photos/schedule", data={"photo_id": "1", "start": "soon", "every_days": "x"})
    assert r.status_code == 400


def _questionnaire(client) -> dict:
    client.post("/crm/questionnaires", data={"name": "Onboarding", "questions": "Your services | longtext"})
    return db.one("SELECT * FROM questionnaires")


def test_questionnaire_ignores_forged_client_id(client, monkeypatch):
    co_id, _, _ = _client_company(client)
    q = _questionnaire(client)
    ingested = []
    monkeypatch.setattr(brain, "ingest", lambda *a, **k: ingested.append(a))
    client.post(f"/q/{q['token']}", data={"q1": "spam", "co": str(co_id)})  # edited link, no signature
    r = db.one("SELECT * FROM questionnaire_responses")
    assert r["company_id"] is None and not ingested
    path = link(q["token"], company_id=co_id)
    page = client.get(path).text
    sig = path.rsplit("s=", 1)[1]
    assert f'value="{sig}"' in page
    client.post(f"/q/{q['token']}", data={"q1": "Drain cleaning", "co": str(co_id), "s": sig})
    assert db.one("SELECT company_id FROM questionnaire_responses ORDER BY id DESC LIMIT 1")["company_id"] == co_id
    assert len(ingested) == 1
    assert not security.signed(sig, "q", q["token"], "", co_id + 1)


def test_questionnaire_saves_answers_when_the_vault_fails(client, monkeypatch):
    co_id, _, _ = _client_company(client)
    q = _questionnaire(client)

    def broken(*a, **k):
        raise brain.BrainError("vault is locked")
    monkeypatch.setattr(brain, "ingest", broken)
    path = link(q["token"], company_id=co_id)
    r = client.post(f"/q/{q['token']}", data={"q1": "x", "co": str(co_id), "s": path.rsplit("s=", 1)[1]})
    assert r.status_code == 200 and db.one("SELECT COUNT(*) n FROM questionnaire_responses")["n"] == 1
    assert db.one("SELECT * FROM events WHERE kind = 'failure' AND action = 'add_to_vault'")


def test_update_plan_shows_brain_error_instead_of_crashing(client, monkeypatch):
    co_id, _, _ = _client_company(client)

    def broken(*a, **k):
        raise brain.BrainError("script failed")
    monkeypatch.setattr(brain, "synthesize", broken)
    r = client.post(f"/clients/{co_id}/synthesize")
    assert r.status_code == 502 and "script failed" in r.text


# --- B. doing things twice or never ------------------------------------------------------------------

import threading  # noqa: E402

import httpx  # noqa: E402

from app import jobs, mailer  # noqa: E402
from app.modules.crm import automations, pipeline  # noqa: E402


def _fake_smtp(monkeypatch):
    sent = []

    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self): pass
        def login(self, *a): pass
        def send_message(self, msg): sent.append(msg["To"])
    monkeypatch.setenv("SMTP_HOST", "smtp.example")
    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    return sent


def test_email_is_sent_once_even_when_approve_races_the_sender(client, monkeypatch):
    sent = _fake_smtp(monkeypatch)
    db.set_setting("brand_address", "1 Main St, Denver CO")  # required before approving outreach
    cid = db.insert("contacts", business_name="Acme", email="a@x.com", source="manual", status="new", tags=[])
    oid = mailer.queue("email", cid, "a@x.com", "Hi", "Hello", origin="manual")
    client.post(f"/crm/outbox/{oid}/approve")
    mailer.send_approved()        # the background sender right after
    client.post(f"/crm/outbox/{oid}/approve")  # back button / double click
    assert sent == ["a@x.com"] and db.one("SELECT status FROM outbox")["status"] == "sent"


def test_send_interrupted_by_closing_is_flagged_not_resent(monkeypatch):
    sent = _fake_smtp(monkeypatch)
    oid = db.insert("outbox", channel="email", to_addr="a@x.com", subject="s", body="b", status="sending", origin="manual")
    mailer.interrupted_sends()
    mailer.send_approved()
    assert sent == [] and db.one("SELECT status FROM outbox WHERE id = ?", oid)["status"] == "failed"


def test_report_approved_twice_queues_emails_once(client):
    co_id, _, _ = _client_company(client)
    client.post(f"/clients/{co_id}/reports", data={"name": "Monthly", "recipients": "owner@example.com"})
    rep = db.one("SELECT * FROM reports")
    client.post(f"/reports/{rep['id']}/build")
    run_id = db.one("SELECT id, status FROM report_runs")["id"]
    db.update("report_runs", run_id, status="pending_approval")
    client.post(f"/reports/runs/{run_id}/approve")
    client.post(f"/reports/runs/{run_id}/approve")
    assert db.one("SELECT COUNT(*) n FROM outbox WHERE origin = 'report'")["n"] == 1


def _threaded_jobs(monkeypatch):
    """Run submitted jobs on a new thread, like the real scheduler (tests normally run them inline)."""
    def submit(fn, *args, **kw):
        t = threading.Thread(target=fn, args=args, kwargs=kw)
        t.start()
        t.join()
    monkeypatch.setattr(jobs, "submit", submit)


def test_automation_loop_stops_on_background_threads(monkeypatch):
    _threaded_jobs(monkeypatch)
    stages = pipeline.pipelines_with_stages()[0]["stages"]
    s1, s2 = stages[1]["id"], stages[2]["id"]
    db.insert("automations", name="a", trigger="deal_stage", trigger_config={"stage_id": s1},
              actions=[{"type": "move_deal", "stage_id": s2}], active=1)
    db.insert("automations", name="b", trigger="deal_stage", trigger_config={"stage_id": s2},
              actions=[{"type": "move_deal", "stage_id": s1}], active=1)
    cid = db.insert("contacts", business_name="Acme", source="manual", status="new", tags=[])
    did = pipeline.create_deal(cid, "Loop")
    pipeline.move_deal(did, s1)
    assert db.one("SELECT COUNT(*) n FROM automation_runs")["n"] <= automations.MAX_DEPTH + 1


def test_interrupted_automation_run_resumes_on_startup():
    cid = db.insert("contacts", business_name="Acme", source="manual", status="new", tags=[])
    aid = db.insert("automations", name="t", trigger="manual", actions=[{"type": "add_tag", "tag": "x"}], active=1)
    rid = db.insert("automation_runs", automation_id=aid, contact_id=cid, step=0, status="running", context={}, log=[])
    automations.resume_interrupted()
    assert db.one("SELECT status FROM automation_runs WHERE id = ?", rid)["status"] == "done"


def test_stopped_run_does_not_continue(monkeypatch):
    cid = db.insert("contacts", business_name="Acme", source="manual", status="new", tags=[])
    aid = db.insert("automations", name="t", trigger="manual",
                    actions=[{"type": "add_tag", "tag": "a"}, {"type": "add_tag", "tag": "b"}], active=1)
    rid = db.insert("automation_runs", automation_id=aid, contact_id=cid, step=0, status="running", context={}, log=[])
    real = automations.execute

    def stop_after_first(action, contact, ctx):
        out = real(action, contact, ctx)
        db.update("automation_runs", rid, status="stopped")  # you press Stop meanwhile
        return out
    monkeypatch.setattr(automations, "execute", stop_after_first)
    automations.advance(rid)
    assert db.one("SELECT status FROM automation_runs WHERE id = ?", rid)["status"] == "stopped"
    assert json.loads(db.one("SELECT tags FROM contacts")["tags"]) == ["a"]


def test_post_timeout_marks_failed_and_is_not_reposted(client, monkeypatch):
    from app.modules.gbp import posts
    from app.providers import gbp_api
    co_id, _, _ = _client_company(client)
    db.update("companies", co_id, gbp_location="accounts/1/locations/2")
    monkeypatch.setattr(gbp_api, "connected", lambda: True)
    calls = []

    def timeout(*a, **k):
        calls.append(1)
        raise gbp_api.GBPError("Could not reach Google: ReadTimeout")
    monkeypatch.setattr(gbp_api, "create_post", timeout)
    pid = db.insert("gbp_posts", company_id=co_id, body="Hi", status="scheduled", scheduled_at="2020-01-01T00:00:00+00:00")
    posts.publish_scheduled()
    posts.publish_scheduled()
    p = db.one("SELECT * FROM gbp_posts WHERE id = ?", pid)
    assert len(calls) == 1 and p["status"] == "failed" and "may have posted" in p["error"]
    assert db.one("SELECT * FROM events WHERE kind = 'failure' AND action = 'publish_scheduled'")


def test_network_errors_become_provider_errors(monkeypatch):
    from app.providers import dataforseo, gbp_api, places

    def down(request):
        raise httpx.ConnectError("offline")
    http = httpx.Client(transport=httpx.MockTransport(down))
    import pytest
    monkeypatch.setenv("DATAFORSEO_LOGIN", "x")
    monkeypatch.setenv("DATAFORSEO_PASSWORD", "y")
    db.run("INSERT INTO oauth_tokens (provider, data, updated_at) VALUES ('google', ?, ?)",
           json.dumps({"access_token": "t", "expires_at": 9e12}), db.now())
    with pytest.raises(gbp_api.GBPError, match="Could not reach Google"):
        gbp_api.call("GET", "https://mybusiness.googleapis.com/x", http)
    with pytest.raises(places.PlacesError):
        places.text_search("plumber", client=http)
    with pytest.raises(dataforseo.DataForSEOError):
        dataforseo._post("/v3/x", [{}], http)
    ok = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"status_code": 40200, "status_message": "Payment required", "tasks": None})))
    with pytest.raises(dataforseo.DataForSEOError, match="40200"):
        dataforseo._post("/v3/x", [{}], ok)


def test_revoked_google_token_disconnects(monkeypatch):
    from app.providers import gbp_api
    import pytest
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "id")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "secret")
    db.run("INSERT INTO oauth_tokens (provider, data, updated_at) VALUES ('google', ?, ?)",
           json.dumps({"access_token": "t", "refresh_token": "r", "expires_at": 0}), db.now())
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(400, json={"error": "invalid_grant"})))
    with pytest.raises(gbp_api.GBPError, match="Reconnect"):
        gbp_api.call("GET", "https://mybusiness.googleapis.com/x", http)
    assert not gbp_api.connected()


def test_weekly_reflection_runs_each_area_separately(monkeypatch):
    from app import journal
    from app.modules.journal import routes as jroutes
    monkeypatch.setattr(ai, "available", lambda **k: True)
    for area in journal.DRAFT_AREAS:
        for _ in range(3):
            journal.record(area, "decision", "approve", "edited", data={"draft": "a", "final": "b"})
    done = []

    def propose(area, company_id=None):
        if area == "posts":
            raise ai.AIUnavailable("limit")
        done.append(area)
    monkeypatch.setattr(journal, "propose_lessons", propose)
    jroutes.weekly_reflection()
    assert set(done) == set(journal.DRAFT_AREAS) - {"posts"}
    assert db.one("SELECT * FROM events WHERE kind = 'failure' AND action = 'propose_lessons'")


# --- C. AI layer ----------------------------------------------------------------------------------

from datetime import datetime, timedelta, timezone  # noqa: E402

from app.ai import base, ollama  # noqa: E402
from tests.test_ai_platforms import fakes  # noqa: E402,F401  (fixture)


def test_empty_reply_falls_back(fakes, monkeypatch, tmp_path):
    import os
    import sys
    exe = tmp_path / ("claude.cmd" if os.name == "nt" else "claude")
    if os.name == "nt":
        exe.write_text("@exit /b 0\r\n", encoding="utf-8")
    else:
        exe.write_text("#!/bin/sh\nexit 0\n")
        exe.chmod(0o755)
    monkeypatch.setenv("CLAUDE_PATH", str(exe))
    assert ai.ask("Hi") == "codex says hello"
    assert ai.paused("claude") is None  # an empty reply isn't a limit


def test_overloaded_falls_back_without_pausing(fakes, monkeypatch):
    monkeypatch.setenv("FAKE_MODE_CLAUDE", "limit")
    monkeypatch.setenv("FAKE_LIMIT_TEXT", "API Error: 529 {\"type\":\"overloaded_error\"}")
    assert ai.ask("Hi") == "codex says hello"
    assert ai.paused("claude") is None
    assert base.classify("429 Too Many Requests") == "limit"


def test_only_respects_switched_off(fakes):
    ai.set_order(["codex", "gemini"])
    import pytest
    with pytest.raises(ai.AIUnavailable, match="switched off"):
        ai.ask("Hi", only="claude")


def test_auth_pause_survives_settings_page(client, fakes, monkeypatch):
    monkeypatch.setenv("FAKE_MODE_GEMINI", "gemini-auth")
    ai.set_order(["gemini"])
    import pytest
    with pytest.raises(ai.AIUnavailable):
        ai.ask("Hi")
    monkeypatch.setattr(ai.PLATFORMS["gemini"], "signed_in", lambda: True)  # an old credentials file exists
    client.get("/settings")
    assert ai.paused("gemini")["kind"] == "auth"


def test_ollama_host_forms(monkeypatch):
    for value, url in [("0.0.0.0", "http://127.0.0.1:11434"), ("0.0.0.0:11434", "http://127.0.0.1:11434"),
                       ("http://192.168.1.5:9000", "http://192.168.1.5:9000"), ("localhost", "http://localhost:11434")]:
        monkeypatch.setenv("OLLAMA_HOST", value)
        assert ollama.base_url() == url, value


def test_retry_time_units_and_weekly_dates():
    now = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
    assert base.retry_time("try again in 5 data centers", now) is None
    assert base.retry_time("try again in 10ms", now) is None
    assert base.retry_time("try again in 3 hours", now) == now + timedelta(hours=3)
    at = base.retry_time("Weekly limit reached, resets Oct 20 at 3pm", now).astimezone()
    assert (at.month, at.day, at.hour) == (10, 20, 15)


# --- D. crash-proofing, public routes, data correctness ------------------------------------------

from app import places_store  # noqa: E402
from app.modules.rank import engine as rank  # noqa: E402


def test_missing_rows_give_404_not_500(client):
    for path in ("/crm/tasks/999/done", "/crm/deals/999/move", "/crm/automations/999/duplicate", "/crm/runs/999/stop",
                 "/crm/submissions/999/not-spam", "/audit/999/share", "/reports/999/delete",
                 "/clients/999/posts/manual"):
        r = client.post(path, data={"stage_id": "1", "body": "x"})
        assert r.status_code == 404, (path, r.status_code)


def test_bad_input_gives_a_message_not_500(client):
    co_id, _, _ = _client_company(client)
    assert client.get("/leads/search?niche=x&area=Denver&lat=39.7&lng=-104.9&max_reviews=10.5&max_rating=abc").status_code == 200
    assert client.post(f"/clients/{co_id}/posts/schedule-check", data={}).status_code in (404, 405)  # route absent: fine
    assert client.get("/journal?days=99999999").status_code == 200
    assert client.get(f"/clients/{co_id}/insights?days=99999999").status_code == 200
    r = client.post("/crm/automations/0", data={"name": "x", "trigger": "manual", "actions": "[1]"})
    assert r.status_code == 400
    r = client.post("/tools/run", data={"prompt": "../../../README"})
    assert r.status_code == 400


def test_review_link_edge_cases_on_public_pages(client):
    co_id, _, _ = _client_company(client)
    cust = db.insert("contacts", company_id=co_id, first_name="Sam", email="sam@x.com", source="manual", status="client")
    client.post(f"/clients/{co_id}/review-requests", data={"contact_id": cust})
    rr = db.one("SELECT * FROM review_requests")
    db.update("companies", co_id, review_link=None)
    assert client.get(f"/r/{rr['token']}/go", follow_redirects=False).status_code == 404
    db.update("review_requests", rr["id"], status="stopped")
    assert client.post(f"/r/{rr['token']}/feedback", data={"message": "x"}).status_code == 404
    assert db.one("SELECT COUNT(*) n FROM feedback")["n"] == 0


def test_scraped_links_only_render_http(client):
    from app.web import _safe_url
    assert _safe_url("javascript:alert(1)") == "" and _safe_url(" https://acme.example ") == "https://acme.example"


def test_save_lead_needs_post(client):
    pid = places_store.upsert({"google_place_id": "ChIJs", "name": "Acme", "lat": 1.0, "lng": 1.0})
    assert client.get(f"/leads/save?place_id={pid}").status_code == 405
    r = client.post("/leads/save", data={"place_id": pid}, follow_redirects=False)
    assert r.status_code == 303 and db.one("SELECT COUNT(*) n FROM contacts")["n"] == 1


def test_first_sync_of_old_reviews_is_not_new(client):
    from app import notify
    co_id, _, _ = _client_company(client)
    db.set_setting("notices_seen", "2026-10-01T00:00:00+00:00")
    db.insert("reviews", company_id=co_id, external_id="old", rating=5, text="2019", review_date="2019-05-01",
              created_at=db.now())
    assert not [n for n in notify.notices() if "new review" in n["text"]]
    db.insert("reviews", company_id=co_id, external_id="new", rating=5, text="now",
              review_date=datetime.now(timezone.utc).date().isoformat(), created_at=db.now())
    assert [n for n in notify.notices() if n["text"].startswith("1 new review")]


def test_late_biweekly_schedule_keeps_14_day_rhythm():
    pid = places_store.upsert({"name": "X", "lat": 1.0, "lng": 1.0})
    due = (datetime.now(timezone.utc) - timedelta(days=3)).replace(microsecond=0)
    sid = db.insert("scan_schedules", name="s", place_id=pid, keywords=["x"], surface="google_maps", provider="demo",
                    grid={"size": 3, "spacing_m": 1000}, recurrence="biweekly", day=due.weekday(),
                    next_run_at=due.isoformat(timespec="seconds"))
    rank.run_schedule(sid)
    nxt = datetime.fromisoformat(db.one("SELECT next_run_at FROM scan_schedules WHERE id = ?", sid)["next_run_at"])
    assert nxt - due == timedelta(days=14)


def test_mcp_scan_is_left_to_the_running_app(monkeypatch):
    from app import mcp_server
    pid = places_store.upsert({"name": "X", "lat": 1.0, "lng": 1.0})
    db.set_setting("app_heartbeat", str(__import__("time").time()))  # the app is running
    out = mcp_server.start_scan(pid, "plumber", provider="demo", grid_size=3)
    assert db.one("SELECT status FROM scans WHERE id = ?", out["id"])["status"] == "queue"
    db.run("UPDATE scans SET created_at = '2000-01-01T00:00:00+00:00'")
    rank.run_queued()  # the app's tick picks it up
    assert db.one("SELECT status FROM scans WHERE id = ?", out["id"])["status"] == "completed"


def test_gbp_lists_follow_page_tokens():
    from app.providers import gbp_api
    pages = {"": {"locations": [{"name": "locations/1", "title": "A"}], "nextPageToken": "p2"},
             "p2": {"locations": [{"name": "locations/2", "title": "B"}]}}

    def handler(request):
        if "accounts" in request.url.path and "locations" not in request.url.path:
            return httpx.Response(200, json={"accounts": [{"name": "accounts/9"}]})
        return httpx.Response(200, json=pages[request.url.params.get("pageToken", "")])
    db.run("INSERT INTO oauth_tokens (provider, data, updated_at) VALUES ('google', ?, ?)",
           json.dumps({"access_token": "t", "expires_at": 9e12}), db.now())
    locs = gbp_api.list_locations(httpx.Client(transport=httpx.MockTransport(handler)))
    assert [loc["title"] for loc in locs] == ["A", "B"]
