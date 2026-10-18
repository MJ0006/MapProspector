"""Configuration from .env. Every optional module checks `enabled(...)` and hides itself when off."""
import os
import shutil
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
# The desktop app keeps its .env next to the data, in the user's app-data folder.
ENV_FILE = Path(os.environ.get("MAPPROSPECTOR_ENV_FILE") or ROOT / ".env")
load_dotenv(ENV_FILE)


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
        "claude": claude_exe() is not None,
        "openai": bool(env("OPENAI_API_KEY")),
        "perplexity": bool(env("PERPLEXITY_API_KEY")),
        "gemini": bool(env("GEMINI_API_KEY")),
        "grok": bool(env("XAI_API_KEY")),
        "smtp": bool(env("SMTP_HOST")),
    }


def find_cli(name: str, override: str) -> str | None:
    """Full path of an AI command-line tool (`claude`, `codex`, `gemini`). Apps started from the Start menu
    can have an older PATH than a fresh terminal, so the usual install folders are checked too.
    The `override` setting (e.g. CLAUDE_PATH) wins when set."""
    if env(override):
        return env(override) if Path(env(override)).is_file() else None
    found = shutil.which(name)  # on Windows this also resolves npm's "<name>.cmd" shims
    if found:
        return found
    home = Path.home()
    for c in (home / ".local" / "bin" / f"{name}.exe", home / ".local" / "bin" / name,
              Path(os.environ.get("APPDATA", home)) / "npm" / f"{name}.cmd"):
        if c.is_file():
            return str(c)
    return None


def claude_exe() -> str | None:
    return find_cli("claude", "CLAUDE_PATH")


def enabled(name: str) -> bool:
    return modules().get(name, False)


def ensure_dirs() -> None:
    for d in (DATA_DIR, VAULT_DIR, UPLOAD_DIR):
        d.mkdir(parents=True, exist_ok=True)
