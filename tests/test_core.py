from app import ai, brain, db


def test_pages_render(client):
    for url in ("/", "/settings"):
        r = client.get(url)
        assert r.status_code == 200, url
    assert "Theme" in client.get("/").text


def test_branding_saved(client):
    r = client.post("/settings", data={"brand_name": "Acme SEO", "brand_color": "#ff0000"}, follow_redirects=True)
    assert r.status_code == 200
    assert db.get_setting("brand_name") == "Acme SEO"
    assert "Acme SEO" in client.get("/").text


def test_extract_json_handles_fences_and_prose():
    assert ai.extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert ai.extract_json('Sure! [1, 2] done') == [1, 2]


def test_brain_prompt_note_parsed():
    p = brain.prompt_note("gbp-description-claude-prompt-1")
    assert p["task"] and p["title"]
    prompt = brain.build_prompt("gbp-description-claude-prompt-1", {"Business name": "Acme Plumbing"})
    assert "Acme Plumbing" in prompt and "gated reviews" in prompt
    assert len(brain.list_prompts()) >= 30


def test_brain_vault_pipeline(isolated):
    fx = brain.settings.BRAIN_DIR / "tests" / "fixtures"
    brain.create_vault("demo-plumbing", "Tester")
    for kind, name in [("gbp", "sample-gbp.csv"), ("reviews", "sample-reviews.json"),
                       ("citations", "sample-citations.csv"), ("geogrid", "sample-geogrid.csv")]:
        brain.ingest(kind, "demo-plumbing", fx / name)
    brain.synthesize("demo-plumbing")
    html = brain.render_report("demo-plumbing")
    assert html.exists()
    assert brain.deliverable("demo-plumbing", "Health Scorecard")
    assert brain.vault_sources("demo-plumbing")
