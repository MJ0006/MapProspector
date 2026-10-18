"""Configuration from .env. Every optional module checks `enabled(...)` and hides itself when off."""
import os
import shutil
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


DATA_DIR = Path(env("DATA_DIR") or ROOT / "data")
DB_PATH = DATA_DIR / "mapprospector.db"
VAULT_DIR = DATA_DIR / "vaults"
UPLOAD_DIR = DATA_DIR / "uploads"
BRAIN_DIR = ROOT / "vendor" / "local-seo-brain"
PUBLIC_BASE_URL = env("PUBLIC_BASE_URL", "http://127.0.0.1:8000").rstrip("/")


def modules() -> dict[str, bool]:
    """Which optional integrations are configured right now."""
    return {
        "places": bool(env("GOOGLE_API_KEY")),
        "dataforseo": bool(env("DATAFORSEO_LOGIN") and env("DATAFORSEO_PASSWORD")),
        "google_oauth": bool(env("GOOGLE_OAUTH_CLIENT_ID") and env("GOOGLE_OAUTH_CLIENT_SECRET")),
        "claude": shutil.which("claude") is not None,
        "openai": bool(env("OPENAI_API_KEY")),
        "perplexity": bool(env("PERPLEXITY_API_KEY")),
        "gemini": bool(env("GEMINI_API_KEY")),
        "grok": bool(env("XAI_API_KEY")),
        "smtp": bool(env("SMTP_HOST")),
    }


def enabled(name: str) -> bool:
    return modules().get(name, False)


def ensure_dirs() -> None:
    for d in (DATA_DIR, VAULT_DIR, UPLOAD_DIR):
        d.mkdir(parents=True, exist_ok=True)
