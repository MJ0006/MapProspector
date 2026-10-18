"""Regression tests for the third full audit (one test per fixed defect)."""
import time

import httpx
import pytest

from app import ai, db, envfile, security, settings
from app.ai import ollama
from app.modules.aivis import engine as aivis
from app.modules.audit import checks
from app.modules.citations import engine as citations
from app.modules.crm import automations, pipeline, spam
from app.modules.crm.forms_routes import ingest_submission
from app.modules.gbp.reviews import iso_date
from app.modules.rank import engine as rank
from app.modules.reports import builder
from app.providers import ai_engines
from app.web import csv_cell
from tests.test_clients_gbp import _client_company


# --- A. sends twice, loses work or leaks -----------------------------------------------------------

def test_automation_restart_never_repeats_a_finished_step():
    cid = db.insert("contacts", business_name="X", email="x@example.com", created_at=db.now())
    aid = db.insert("automations", name="a", trigger="manual", actions=[
        {"type": "create_task", "title": "first"}, {"type": "create_task", "title": "second"}], created_at=db.now())
    # the app closed after step 1 was saved: recovery continues with step 2 only
    db.insert("tasks", contact_id=cid, title="first", created_at=db.now())
    db.insert("automation_runs", automation_id=aid, contact_id=cid, step=1, status="running", context={}, log=[],
              created_at=db.now(), updated_at=db.now())
    automations.resume_interrupted()
    assert sorted(t["title"] for t in db.all("SELECT title FROM tasks")) == ["first", "second"]
    assert db.one("SELECT status FROM automation_runs")["status"] == "done"


def test_interrupted_report_send_finishes_once_per_recipient(client):
    co_id, _, _ = _client_company(client)
    rid = db.insert("reports", company_id=co_id, name="Monthly", sections=["rank"], recipients="a@x.com, b@x.com",
                    schedule="none", require_approval=0, created_at=db.now())
    run_id = db.insert("report_runs", report_id=rid, html="<p>r</p>", token="tok123", status="sending", created_at=db.now())
    db.insert("outbox", channel="email", to_addr="a@x.com", subject="s", body="link /share/report/tok123",
              status="sent", origin="report", created_at=db.now())
    builder.interrupted_report_sends()
    builder.interrupted_report_sends()
    sent = [r["to_addr"] for r in db.all("SELECT to_addr FROM outbox WHERE origin = 'report' ORDER BY id")]
    assert sent == ["a@x.com", "b@x.com"]
    assert db.one("SELECT status FROM report_runs WHERE id = ?", run_id)["status"] == "sent"


def test_engine_errors_are_stored_without_urls_or_keys():
    assert "SECRET" not in aivis.safe_error(ValueError("GET https://x.test/?key=SECRET failed; key=SECRET"))
    assert "http" not in aivis.safe_error(ValueError("error at https://api.example.com/v1?token=abc"))


def test_two_clients_with_the_same_name_get_their_own_vault(client):
    first, _, _ = _client_company(client)
    second, _, _ = _client_company(client, google_place_id="ChIJother")
    slugs = [db.one("SELECT vault_slug FROM companies WHERE id = ?", i)["vault_slug"] for i in (first, second)]
    assert slugs[0] != slugs[1]


def test_publish_now_while_publishing_is_refused(client):
    co_id, _, _ = _client_company(client)
    pid = db.insert("gbp_posts", company_id=co_id, body="Hi", status="publishing", created_at=db.now())
    assert client.post(f"/gbp/posts/{pid}/publish").status_code == 409


# --- B. security -----------------------------------------------------------------------------------

@pytest.mark.parametrize("url", ["http://127.0.0.1/", "http://169.254.169.254/latest/", "http://localhost:8000/",
                                 "http://10.0.0.5/", "file:///etc/passwd"])
def test_website_audit_refuses_private_addresses(url):
    with pytest.raises(httpx.RequestError):
        checks.only_public(httpx.Request("GET", url))


def test_pages_cannot_be_framed_by_other_sites(client):
    r = client.get("/")
    assert r.headers["x-frame-options"] == "SAMEORIGIN" and "frame-ancestors 'self'" in r.headers["content-security-policy"]


def test_login_locks_out_after_ten_wrong_passwords_and_sessions_expire(client, monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "right")
    monkeypatch.setattr(security, "_failures", {})
    for _ in range(security.MAX_FAILURES):
        assert "Wrong password" in client.post("/login", data={"password": "nope"}).text
    r = client.post("/login", data={"password": "right"}, follow_redirects=False)
    assert r.status_code != 303 and "Too many" in r.text
    old = int(time.time()) - 1
    assert not security.valid_session(f"{old}.n.{security._session_mac(old, 'n')}")
    assert security.valid_session(security.session_value())
    cookie = security.session_value()
    monkeypatch.setenv("APP_PASSWORD", "changed")  # a new password logs everyone out
    assert not security.valid_session(cookie)


def test_api_refuses_cross_site_requests(client):
    r = client.get("/api/places/search?q=x", headers={"sec-fetch-site": "cross-site"})
    assert r.status_code == 403


def test_public_form_keeps_one_email_and_caps_submissions_per_ip():
    form = {"id": db.insert("forms", name="F", token="t", fields=[], created_at=db.now()), "company_id": None,
            "name": "F"}
    first = ingest_submission(form, {"name": "A B", "email": "a@x.com, victim@y.com"}, "1.2.3.4", None)
    assert db.one("SELECT email FROM contacts WHERE id = ?", first["contact_id"])["email"] == "a@x.com"
    results = [ingest_submission(form, {"name": "A", "email": "a@x.com"}, "1.2.3.4", None) for _ in range(5)]
    assert not results[3]["spam"] and results[4]["spam"]


def test_csv_cells_never_start_a_formula():
    assert csv_cell("=HYPERLINK(1)") == "'=HYPERLINK(1)" and csv_cell("@x") == "'@x" and csv_cell("Acme") == "Acme"
    assert csv_cell(None) == ""


# --- C. wrong results ------------------------------------------------------------------------------

def test_aivis_parsing():
    text = 'Here are some.\n{"businesses": [{"name": "Acme Plumbing", "reason": "r"}]}\nSources: https://a.com'
    assert [b["name"] for b in aivis.parse_answer(text)] == ["Acme Plumbing"]
    listed = "1. **Smith-Jones Plumbing** - great\n2. [yelp](https://yelp.com)\n- https://x.com\n3. Best Drain Co"
    assert [b["name"] for b in aivis.parse_answer(listed)] == ["Smith-Jones Plumbing", "Best Drain Co"]
    assert aivis.parse_answer('{"businesses": null}') == []
    assert aivis.same_business("Acme Plumbing", "Acme Plumbing & Drain")
    assert not aivis.same_business("The Plumber", "Joe the Plumber")


def test_ai_visibility_uses_each_subscription_with_web_search_and_no_fallback(monkeypatch):
    calls = []
    monkeypatch.setattr(ai, "ask", lambda prompt, **k: calls.append(k) or "Acme https://acme.com\n{}")
    for engine, platform in [("claude", "claude"), ("chatgpt", "codex"), ("gemini", "gemini")]:
        assert ai_engines.ask(engine, "q")["sources"] == ["https://acme.com"]
        assert calls[-1]["only"] == platform and calls[-1]["web"] is True
    assert set(ai_engines.ENGINES) == {"claude", "chatgpt", "gemini"}
    assert ai_engines.LABELS["grok"] == "Grok"  # older checks still show their engine names


def test_extract_json_prefers_the_real_object_over_a_footnote():
    assert ai.extract_json('See [1].\n{"listings": [{"directory": "Yelp"}]}') == {"listings": [{"directory": "Yelp"}]}


def test_citation_addresses():
    p = citations.profile_from("Acme", "123 Main St, Suite 4, Springfield, IL 62701, USA", "", "")
    assert (p["street"], p["city"], p["state"], p["zip"]) == ("123 Main St, Suite 4", "Springfield", "IL", "62701")
    p = citations.profile_from("Acme", "Springfield, IL, USA", "", "")
    assert (p["street"], p["city"], p["state"]) == ("", "Springfield", "IL")
    nap = {"name": "Acme", "street": "12 Main St", "zip": "62701"}
    assert citations.compare(nap, {"name": "Acme"})["address"]["status"] == "unknown"
    assert citations.compare(nap, {"name": "Acme", "address": "112 Main St, Springfield 62701"})["address"]["status"] == "mismatch"
    assert citations.compare(nap, {"name": "Acme", "address": "12 Main Street, Springfield 62701"})["address"]["status"] == "match"


def test_rank_change_compares_like_for_like_scans_only():
    base = dict(place_id=None, keyword="roofer", surface="google_maps", grid_size=3, shape="square", status="completed")
    a = db.insert("scans", provider="demo", **base)
    b = db.insert("scans", provider="places", **base)
    c = db.insert("scans", provider="places", **base)
    assert rank.previous_scan(db.one("SELECT * FROM scans WHERE id = ?", c))["id"] == b
    assert rank.previous_scan(db.one("SELECT * FROM scans WHERE id = ?", b)) is None
    assert a


def test_completed_tasks_use_their_done_date(client):
    tid = db.insert("tasks", title="t", created_at="2020-01-01T00:00:00+00:00")
    client.post(f"/crm/tasks/{tid}/done")
    assert db.one("SELECT done_at FROM tasks WHERE id = ?", tid)["done_at"]


def test_audit_title_brand_and_city():
    html = ('<html><head><title>Summit Roofing | Denver roof repair</title></head><body><svg><title>icon</title></svg>'
            '<h1>Hi</h1></body></html>')
    out = {c["key"]: c for c in checks.page_checks("https://s.com", html, "https://s.com", 200, 0.3,
                                                     {"name": "The Summit Roofing Co"})}
    assert "Summit Roofing | Denver" in out["title"]["detail"] and out["title_brand"]["status"] == "pass"
    assert checks._city("1 Main St, Austin, TX 78701, USA") == "Austin"
    assert checks._city("10 Downing St, London SW1A 2AA, UK") == "London"


def test_spam_rules_match_whole_words_and_exact_domains():
    db.insert("spam_rules", kind="keyword", value="sex", active=1)
    db.insert("spam_rules", kind="domain", value="spam.com", active=1)
    assert not spam.check({"message": "We are in Essex"})[0]
    assert not spam.check({"email": "me@nospam.com"})[0]
    assert spam.check({"email": "me@mail.spam.com"})[0]


def test_custom_won_and_lost_stages_set_the_deal_status():
    assert pipeline.stage_status({"name": "Closed Won", "win_pct": 90}) == "won"
    assert pipeline.stage_status({"name": "lost", "win_pct": 0}) == "lost"
    assert pipeline.stage_status({"name": "Lost touch", "win_pct": 0}) == "open"


def test_review_dates_are_stored_as_iso():
    assert iso_date("03/04/2026") == iso_date("Mar 4, 2026") == iso_date("2026-03-04T10:00:00Z") == "2026-03-04"
    assert iso_date("") is None and iso_date("sometime") is None


# --- D. keys form and small UX ---------------------------------------------------------------------

def test_env_file_clears_duplicates_and_quotes_values(tmp_path):
    path = tmp_path / ".env"
    path.write_text("SMTP_PORT=25\nA=1\nA=2\n", encoding="utf-8")
    envfile.update(path, {"SMTP_PORT": None, "A": "pa ss#$1"})
    assert path.read_text(encoding="utf-8") == "A='pa ss#$1'\n"
    assert envfile.read(path) == {"A": "pa ss#$1"}


def test_keys_set_in_windows_are_labelled_and_left_alone(client, monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "AIzaSYSTEM1234")
    monkeypatch.setattr(settings, "SYSTEM_ENV", frozenset({"GOOGLE_API_KEY"}))
    assert "set in Windows environment variables" in client.get("/settings").text
    client.post("/settings/keys", data={"GOOGLE_API_KEY": "AIzaNEW", "clear_GOOGLE_API_KEY": "1"})
    assert settings.env("GOOGLE_API_KEY") == "AIzaSYSTEM1234" and "GOOGLE_API_KEY" not in envfile.read(settings.ENV_FILE)


def test_failed_email_can_be_retried_and_sending_one_is_left_alone(client):
    failed = db.insert("outbox", channel="email", to_addr="a@x.com", subject="s", body="b", status="failed",
                       error="smtp down", origin="manual", created_at=db.now())
    sending = db.insert("outbox", channel="email", to_addr="a@x.com", subject="s", body="b", status="sending",
                        origin="manual", created_at=db.now())
    client.post(f"/crm/outbox/{failed}/retry")
    client.post(f"/crm/outbox/{sending}/cancel")
    client.post(f"/crm/outbox/{sending}/mark_sent")
    assert db.one("SELECT status FROM outbox WHERE id = ?", failed)["status"] == "draft"
    assert db.one("SELECT status FROM outbox WHERE id = ?", sending)["status"] == "sending"


def test_failed_photo_shows_its_error_and_can_be_rescheduled(client):
    from app.modules.gbp import photos
    co_id, _, _ = _client_company(client)
    pid = db.insert("photos", company_id=co_id, filename="a.jpg", status="publishing", created_at=db.now())
    photos.recover_publishing()
    assert "closed while publishing" in client.get(f"/clients/{co_id}/photos").text
    client.post(f"/clients/{co_id}/photos/schedule", data={"photo_id": str(pid), "start": "2030-01-01T09:00", "every_days": "7"})
    p = db.one("SELECT * FROM photos WHERE id = ?", pid)
    assert p["status"] == "scheduled" and p["error"] is None


def test_ollama_host_ipv6_and_https(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "[::1]:11434")
    assert ollama.base_url() == "http://[::1]:11434"
    monkeypatch.setenv("OLLAMA_HOST", "https://ollama.example.com")
    assert ollama.base_url() == "https://ollama.example.com:443"


def test_old_checks_with_retired_engines_still_render_and_only_ready_platforms_run(client, monkeypatch):
    rows = [{"engine": "grok", "prompt": "q", "text": "1. Acme Plumbing", "sources": [],
             "businesses": [{"name": "Acme Plumbing", "reason": ""}]}]
    report = aivis.build_report({"name": "Acme Plumbing", "website": None}, rows)
    rid = db.insert("aivis_runs", business_name="Acme Plumbing", city="Austin", category="plumber", status="completed",
                    report=report, created_at=db.now())
    assert "Grok" in client.get(f"/aivis/{rid}").text
    monkeypatch.setattr(ai, "candidates", lambda **k: [object()] if k.get("only") == "codex" else [])
    monkeypatch.setattr(ai.PLATFORMS["codex"], "signed_in", lambda: True)
    assert ai_engines.available() == ["chatgpt"]


def test_signed_out_platforms_are_not_offered_and_redirect_sources_are_dropped(monkeypatch):
    monkeypatch.setattr(ai, "candidates", lambda **k: [object()])
    monkeypatch.setattr(ai.PLATFORMS["gemini"], "signed_in", lambda: False)
    monkeypatch.setattr(ai.PLATFORMS["codex"], "signed_in", lambda: None)  # unknown: still offered
    monkeypatch.setattr(ai.PLATFORMS["claude"], "signed_in", lambda: True)
    assert ai_engines.available() == ["claude", "chatgpt"]
    text = ("Sources: https://vertexaisearch.cloud.google.com/grounding-api-redirect/AbC https://acme.com/about "
            "https://www.google.com/url?q=https://x.com\n{}")
    monkeypatch.setattr(ai, "ask", lambda prompt, **k: text)
    assert ai_engines.ask("gemini", "q")["sources"] == ["https://acme.com/about"]
