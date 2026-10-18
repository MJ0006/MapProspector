"""Brain panel: Local SEO Brain's next action, vault lint, evidence age and relationship map for each client."""
from datetime import date, timedelta

from app import brain, db
from app.modules.audit.routes import TOOLS
from tests.test_clients_gbp import _client_company

BRAIN_FX = brain.settings.BRAIN_DIR / "tests" / "fixtures"


def test_brain_tab_shows_next_action_health_and_missing_evidence(client):
    co_id, _, _ = _client_company(client)
    page = client.get(f"/clients/{co_id}/brain")
    assert page.status_code == 200
    assert "Add evidence:" in page.text and "Google Business Profile export" in page.text
    assert "passed" in page.text  # a fresh vault passes the Brain's lint
    assert ".py" not in page.text.split("From the vault's guide:")[-1].split("</ol>")[0]  # operator-only steps dropped
    svg = client.get(f"/clients/{co_id}/brain/map.svg")
    assert svg.status_code == 200 and svg.headers["content-type"].startswith("image/svg+xml") and b"<svg" in svg.content


def test_evidence_age_flags_old_exports(client):
    co_id, _, _ = _client_company(client)
    with open(BRAIN_FX / "sample-gbp.csv", "rb") as f:
        client.post(f"/clients/{co_id}/evidence", data={"kind": "gbp"}, files={"file": ("gbp.csv", f, "text/csv")})
    slug = db.one("SELECT vault_slug FROM companies WHERE id = ?", co_id)["vault_slug"]
    gbp = next(e for e in brain.evidence_age(slug) if e["kind"] == "gbp")
    assert gbp["days"] == 0 and not gbp["stale"]
    note = next((brain.vault_path(slug) / "wiki" / "sources").glob("gbp-*.md"))
    old = (date.today() - timedelta(days=45)).isoformat()
    note.write_text(note.read_text(encoding="utf-8").replace(f'created: "{date.today().isoformat()}"', f'created: "{old}"', 1),
                    encoding="utf-8")
    gbp = next(e for e in brain.evidence_age(slug) if e["kind"] == "gbp")
    assert gbp["days"] == 45 and gbp["stale"]
    assert "Refresh evidence:" in client.get(f"/clients/{co_id}/brain").text


def test_reference_notes_past_review_are_listed(client, monkeypatch):
    co_id, _, _ = _client_company(client)
    monkeypatch.setattr(brain, "_today", lambda: "2099-01-01")
    action = brain.next_action(db.one("SELECT vault_slug FROM companies WHERE id = ?", co_id)["vault_slug"])
    assert action["due"] and not action["steps"]
    page = client.get(f"/clients/{co_id}/brain").text
    assert "reference notes to be reviewed" in page and "prompts (" in page


def test_broken_vault_fails_lint(client):
    co_id, _, _ = _client_company(client)
    slug = db.one("SELECT vault_slug FROM companies WHERE id = ?", co_id)["vault_slug"]
    (brain.vault_path(slug) / "wiki" / "broken.md").write_text("no frontmatter, links to [[Nowhere]]\n", encoding="utf-8")
    health = brain.lint(slug)
    assert not health["ok"] and health["lines"]
    assert "needs fixing" in client.get(f"/clients/{co_id}/brain").text


def test_content_tools_offer_every_brain_prompt(client):
    names = [n for group in TOOLS.values() for n in group]
    assert sorted(names) == sorted(p["name"] for p in brain.list_prompts())
    page = client.get("/tools").text
    for p in brain.list_prompts():
        assert p["title"].strip() in page.replace("&#39;", "'").replace("&amp;", "&")


def test_missing_vault_folder_is_reported_clearly(client):
    import shutil
    co_id, _, _ = _client_company(client)
    slug = db.one("SELECT vault_slug FROM companies WHERE id = ?", co_id)["vault_slug"]
    shutil.rmtree(brain.vault_path(slug))
    assert "vault folder is missing" in client.get(f"/clients/{co_id}/brain").text
    assert client.get(f"/clients/{co_id}/brain/map.svg").status_code == 502
    assert not brain.vault_path(slug).exists()  # nothing half-created


def test_map_cannot_run_scripts_and_top_level_notes_are_grouped(client, monkeypatch):
    co_id, _, _ = _client_company(client)
    csp = client.get(f"/clients/{co_id}/brain/map.svg").headers["content-security-policy"]
    assert "default-src 'none'" in csp and "script" not in csp
    monkeypatch.setattr(brain, "next_action", lambda slug: {"steps": [], "due": ["wiki/hot.md", "google-search-doc-updates",
                                                                              "wiki/prompts/a.md"]})
    page = client.get(f"/clients/{co_id}/brain").text
    assert "vault notes (1)" in page and "Google sources (1)" in page and "prompts (1)" in page


def test_lint_crash_is_an_error_not_a_vault_finding(client, monkeypatch):
    co_id, _, _ = _client_company(client)

    def crash(*a, **k):
        raise brain.BrainError("Traceback (most recent call last):\n  boom")
    monkeypatch.setattr(brain, "_run", crash)
    import pytest
    with pytest.raises(brain.BrainError):
        brain.lint("x")


def test_content_prompts_get_the_content_doctrine(client, monkeypatch):
    from app import ai
    seen = []
    monkeypatch.setattr(ai, "ask", lambda prompt, **k: seen.append(prompt) or "draft")
    for name, canon in [("blog-post-writing-prompt", "002"), ("gbp-services-prompt", "001"), ("claude-prompt-2", "003")]:
        client.post("/tools/run", data={"prompt": name, "business_name": "Acme"})
        want = brain.doctrine(int(canon), 2000)[:200]
        assert want and want in seen[-1], name
