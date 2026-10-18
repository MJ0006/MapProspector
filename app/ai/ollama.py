"""Local models through Ollama (http://127.0.0.1:11434 by default, or OLLAMA_HOST). No sign-in, no limits, no tools."""
import time

import httpx

from app import db, settings
from app.ai.base import Platform, PlatformError

_tags_cache: dict = {}


def base_url() -> str:
    host = settings.env("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
    return host if host.startswith("http") else f"http://{host}"


def client(timeout: float) -> httpx.Client:  # tests swap this for a fake transport
    return httpx.Client(timeout=timeout)


def models() -> list[str] | None:
    """Installed model names, or None when Ollama isn't running. Cached for 30 seconds."""
    hit = _tags_cache.get(base_url())
    if hit and time.monotonic() - hit[0] < 30:
        return hit[1]
    try:
        with client(2) as http:
            r = http.get(base_url() + "/api/tags")
            r.raise_for_status()
            names = [m["name"] for m in r.json().get("models", []) if m.get("name")]
    except (httpx.HTTPError, ValueError, KeyError):
        names = None
    _tags_cache[base_url()] = (time.monotonic(), names)
    return names


def chosen_model() -> str | None:
    names = models() or []
    pick = db.get_setting("ollama_model", "")
    return pick if pick in names else (names[0] if names else None)


class Ollama(Platform):
    name = "ollama"
    label = "Local (Ollama)"
    install = "Install Ollama (ollama.com) and download a model, e.g. `ollama pull llama3.1`."

    def exe(self) -> str | None:
        return base_url() if models() is not None else None

    def signed_in(self) -> bool | None:
        return bool(models())  # running with at least one model

    def run(self, prompt, *, web, mcp, tools, timeout) -> str:
        model = chosen_model()
        if not model:
            raise PlatformError("Ollama is not running or has no models.", "auth")
        try:
            with client(timeout) as http:
                r = http.post(base_url() + "/api/chat", json={"model": model, "stream": False,
                                                            "messages": [{"role": "user", "content": prompt}]})
        except httpx.TimeoutException:
            raise PlatformError(f"{self.label} did not answer within {timeout} seconds.", "timeout")
        except httpx.HTTPError as exc:
            raise PlatformError(f"{self.label}: {exc}")
        if r.status_code >= 400:
            raise PlatformError(f"{self.label}: {r.text[:300]}")
        return str((r.json().get("message") or {}).get("content", "")).strip()
