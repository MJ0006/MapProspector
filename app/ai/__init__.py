"""AI for every draft, Ask answer and lookup: your subscriptions (Claude, ChatGPT, Gemini) and local models.

ask() tries the platforms in your order (Settings → AI platforms) and returns the first answer:
- a platform that reports a usage limit is skipped until its reset time (or an hour);
- one that isn't signed in is skipped for 30 minutes, or until Settings sees it signed in;
- a task that needs web search or your app data only goes to platforms that can do that.
Every attempt is journaled with its platform. Everything returned is a draft for you to approve;
nothing generated is ever sent or published directly.

One file per platform (claude.py, codex.py, gemini.py, ollama.py); add a platform by adding a file
and listing it in PLATFORMS.
"""
import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone

from app import db
from app.ai.base import AIUnavailable, Platform, PlatformError
from app.ai.claude import Claude
from app.ai.codex import Codex
from app.ai.gemini import Gemini
from app.ai.ollama import Ollama

__all__ = ["AIUnavailable", "PlatformError", "PLATFORMS", "ask", "ask_json", "available", "extract_json",
           "last_platform", "order", "set_order", "status", "resume"]

TIMEOUT = 240
LIMIT_PAUSE = timedelta(hours=1)
AUTH_PAUSE = timedelta(minutes=30)
MAX_PAUSE = timedelta(days=8)  # a weekly limit plus a day; never park a platform forever on a misread time

PLATFORMS: dict[str, Platform] = {p.name: p for p in (Claude(), Codex(), Gemini(), Ollama())}
_local = threading.local()


# --- your order and on/off -----------------------------------------------------------------------

def order() -> list[str]:
    """Switched-on platforms, in the order they're tried."""
    saved = db.loads(db.get_setting("ai_order", ""), None)
    names = saved if isinstance(saved, list) else list(PLATFORMS)
    return [n for n in names if n in PLATFORMS]


def set_order(names: list[str]) -> None:
    db.set_setting("ai_order", json.dumps([n for n in dict.fromkeys(names) if n in PLATFORMS]))


# --- pauses after limits / sign-in problems -------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def paused(name: str) -> dict | None:
    p = db.loads(db.get_setting(f"ai_paused_{name}", ""), None)
    if not p:
        return None
    if datetime.fromisoformat(p["until"]) <= _now():
        resume(name)
        return None
    return p


def _pause(name: str, exc: PlatformError) -> None:
    default = LIMIT_PAUSE if exc.kind == "limit" else AUTH_PAUSE
    until = exc.retry_at if exc.retry_at and exc.retry_at > _now() else _now() + default
    until = min(until, _now() + MAX_PAUSE)
    db.set_setting(f"ai_paused_{name}", json.dumps({"until": until.isoformat(timespec="seconds"), "kind": exc.kind,
                                                    "reason": re.sub(r"\|\d{10}\b", "", str(exc))[:300]}))


def resume(name: str) -> None:
    db.set_setting(f"ai_paused_{name}", "")


# --- asking ---------------------------------------------------------------------------------------

def candidates(*, web: bool = False, mcp: bool = False, only: str | None = None) -> list[Platform]:
    names = [only] if only else order()
    return [PLATFORMS[n] for n in names if n in PLATFORMS and (not web or PLATFORMS[n].web)
            and (not mcp or PLATFORMS[n].mcp) and PLATFORMS[n].installed() and not paused(n)]


def available(*, web: bool = False, mcp: bool = False) -> bool:
    return bool(candidates(web=web, mcp=mcp))


def missing_message(*, web: bool = False, mcp: bool = False) -> str:
    need = " that can search the web" if web else " that can read your app data" if mcp else ""
    return (f"No AI platform{need} is ready. Set one up on Settings → AI platforms "
            "(Claude Code, ChatGPT's Codex CLI, Gemini CLI, or Ollama for local drafts).")


def last_platform() -> str | None:
    """The platform that wrote the last answer on this thread (journaled with drafts)."""
    return getattr(_local, "platform", None)


def ask(prompt: str, *, web: bool = False, timeout: int = TIMEOUT, tools: list[str] | None = None,
        mcp: bool = False, purpose: str = "ai", only: str | None = None) -> str:
    """Run one prompt on the first platform that answers.
    web=True: the platform may search the web (citation checks). mcp=True: it may read your MapProspector
    data through read-only MCP tools (Ask); `tools` lists the tools Claude may use without asking.
    only="claude": no fallback (AI visibility measures that one engine)."""
    from app import journal
    tried: list[str] = []
    _local.platform = None
    for p in candidates(web=web, mcp=mcp, only=only):
        t0 = time.monotonic()
        meta = {"purpose": purpose, "platform": p.name, "web": web, "mcp": mcp, "prompt_chars": len(prompt),
                "prompt_head": prompt[:300]}
        try:
            text = p.run(prompt, web=web, mcp=mcp, tools=list(tools or []), timeout=timeout)
        except PlatformError as exc:
            journal.record(purpose, "ai_call", "ask", "failed", detail=f"{p.label}: {exc}",
                           data={**meta, "ms": int((time.monotonic() - t0) * 1000), "error_kind": exc.kind})
            if exc.kind in ("limit", "auth"):
                _pause(p.name, exc)
            tried.append(f"{p.label}: {str(exc)[:200]}")
            continue
        journal.record(purpose, "ai_call", "ask", data={**meta, "ms": int((time.monotonic() - t0) * 1000),
                                                         "reply_chars": len(text)})
        if tried:
            journal.record("ai", "outcome", "fallback", detail=f"{p.label} answered after: " + " | ".join(tried))
        _local.platform = p.name
        return text
    if not tried:
        raise AIUnavailable(f"{PLATFORMS[only].label} is not ready." if only in PLATFORMS
                            else missing_message(web=web, mcp=mcp))
    raise AIUnavailable("No AI platform could answer. " + " | ".join(tried))


def ask_json(prompt: str, *, web: bool = False, timeout: int = TIMEOUT, purpose: str = "ai"):
    """Ask for JSON and parse the first JSON value in the reply."""
    text = ask(prompt + "\n\nReply with JSON only, no prose, no code fences.", web=web, timeout=timeout,
               purpose=purpose)
    try:
        return extract_json(text)
    except ValueError:
        from app import journal
        journal.failure(purpose, "parse_json", "The AI's reply had no valid JSON",
                        data={"reply": text[:1000], "platform": last_platform()})
        raise


def extract_json(text: str):
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    for opener in ("{", "["):
        start = text.find(opener)
        if start == -1:
            continue
        try:
            return json.JSONDecoder().raw_decode(text[start:])[0]
        except ValueError:
            continue
    raise ValueError("No JSON found in AI reply")


# --- for Settings and the notice bar --------------------------------------------------------------

def status() -> list[dict]:
    """Every platform, switched-on ones first in your order."""
    on = order()
    out = []
    for name in on + [n for n in PLATFORMS if n not in on]:
        p = PLATFORMS[name]
        installed = p.installed()
        pause = paused(name)
        signed = p.signed_in() if installed else False
        if pause and pause["kind"] == "auth" and signed:
            resume(name)  # you signed in since
            pause = None
        out.append({"name": name, "label": p.label, "on": name in on, "installed": installed, "signed_in": signed,
                    "paused": pause, "web": p.web, "mcp": p.mcp, "install": p.install,
                    "signin": " ".join(p.signin) if p.signin else ""})
    return out


def pauses() -> list[dict]:
    """Switched-on platforms that are paused now, with the platform that answers instead."""
    on = order()
    active = [n for n in on if not paused(n) and PLATFORMS[n].installed()]
    out = []
    for name in on:
        p = paused(name)
        if p:
            out.append({"name": name, "label": PLATFORMS[name].label, **p,
                        "instead": PLATFORMS[active[0]].label if active else None})
    return out
