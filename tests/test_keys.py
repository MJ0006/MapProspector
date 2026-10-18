"""Settings → API keys: save keys in the app, masked, local-only, effective without a restart."""
import os

from app import db, envfile, settings


def test_env_file_update_keeps_comments_and_unknown_lines(tmp_path):
    path = tmp_path / ".env"
    path.write_text("# Google\nGOOGLE_API_KEY=\nMY_OWN_THING=keep me\n\n# Mail\nSMTP_PORT=587\n", encoding="utf-8")
    envfile.update(path, {"GOOGLE_API_KEY": "AIzaNEW", "SMTP_PORT": None, "DATAFORSEO_LOGIN": "me@x.com"})
    text = path.read_text(encoding="utf-8")
    assert text == "# Google\nGOOGLE_API_KEY=AIzaNEW\nMY_OWN_THING=keep me\n\n# Mail\nDATAFORSEO_LOGIN=me@x.com\n"
    assert envfile.read(path)["MY_OWN_THING"] == "keep me"
    import pytest
    with pytest.raises(ValueError):
        envfile.update(path, {"GOOGLE_API_KEY": "a\nEVIL=1"})


def test_saving_a_key_turns_the_module_on_without_restart(client, monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    assert not settings.enabled("places")
    r = client.post("/settings/keys", data={"GOOGLE_API_KEY": "AIzaSyTEST1234"}, follow_redirects=False)
    assert r.status_code == 303 and "keys_saved=1" in r.headers["location"]
    assert settings.enabled("places") and os.environ["GOOGLE_API_KEY"] == "AIzaSyTEST1234"
    assert envfile.read(settings.ENV_FILE)["GOOGLE_API_KEY"] == "AIzaSyTEST1234"
    page = client.get("/settings").text
    assert "••••1234" in page and "AIzaSyTEST1234" not in page  # never shown in full


def test_blank_secret_keeps_value_and_clear_removes_it(client, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    client.post("/settings/keys", data={"OPENAI_API_KEY": "sk-abcdef9999"})
    client.post("/settings/keys", data={"OPENAI_API_KEY": ""})
    assert os.environ["OPENAI_API_KEY"] == "sk-abcdef9999"
    client.post("/settings/keys", data={"clear_OPENAI_API_KEY": "1"})
    assert "OPENAI_API_KEY" not in os.environ and not envfile.read(settings.ENV_FILE).get("OPENAI_API_KEY")


def test_plain_fields_show_and_public_url_needs_restart(client, monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    client.post("/settings/keys", data={"SMTP_HOST": "smtp.gmail.com"})
    assert 'value="smtp.gmail.com"' in client.get("/settings").text
    r = client.post("/settings/keys", data={"SMTP_HOST": "smtp.gmail.com", "PUBLIC_BASE_URL": "https://t.example"},
                    follow_redirects=False)
    assert "restart=1" in r.headers["location"]


def test_keys_are_local_only_and_journal_has_names_not_values(client, monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    from app import security
    real = security.is_local
    monkeypatch.setattr(security, "is_local", lambda request: False)  # a request from another computer
    assert client.post("/settings/keys", data={"XAI_API_KEY": "xai-secret"}).status_code == 403
    monkeypatch.setattr(security, "is_local", real)  # (not monkeypatch.undo(): that would undo conftest's isolation)
    client.post("/settings/keys", data={"XAI_API_KEY": "xai-secret"})
    ev = db.one("SELECT * FROM events WHERE action = 'keys_saved'")
    assert ev["detail"] == "XAI_API_KEY" and "xai-secret" not in (ev["data"] or "")
    assert "DATA_DIR" not in client.get("/settings").text.split('id="keys"')[1].split('id="ai"')[0]


def test_tests_never_touch_the_real_env_file():
    assert settings.ENV_FILE != settings.ROOT / ".env"
