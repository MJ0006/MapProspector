import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["MAPPROSPECTOR_NO_JOBS"] = "1"
FIXTURES = Path(__file__).with_name("fixtures")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Fresh data dir + database per test; background jobs run inline."""
    from app import db, jobs, settings
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(settings, "VAULT_DIR", tmp_path / "vaults")
    monkeypatch.setattr(settings, "UPLOAD_DIR", tmp_path / "uploads")
    for key in ("GOOGLE_API_KEY", "DATAFORSEO_LOGIN", "DATAFORSEO_PASSWORD", "OPENAI_API_KEY",
                "PERPLEXITY_API_KEY", "GEMINI_API_KEY", "XAI_API_KEY", "SMTP_HOST",
                "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(jobs, "INLINE", True)
    # Never call the real claude CLI from tests; tests that need AI patch app.ai explicitly.
    import shutil
    real_which = shutil.which
    ai_clis = {"claude", "codex", "gemini"}
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None if name in ai_clis else real_which(name, *a, **k))
    from app.ai import ollama
    monkeypatch.setattr(ollama, "models", lambda: None)  # never reach a real Ollama
    for var in ("HOME", "USERPROFILE", "APPDATA"):  # settings.claude_exe() also looks in these
        monkeypatch.setenv(var, str(tmp_path / "home"))
    for var in ("CLAUDE_PATH", "CODEX_PATH", "GEMINI_PATH", "GEMINI_API_KEY", "OLLAMA_HOST"):
        monkeypatch.delenv(var, raising=False)
    db.init(tmp_path / "test.db")
    yield tmp_path


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        yield c
