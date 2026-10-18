"""Answer engines for the AI visibility checker (A6).

Each engine gets the same local question and returns {"text": answer, "sources": [urls]}. All of them run through
your own subscriptions (Settings → AI platforms), with web search on: Claude (Claude Code), ChatGPT (Codex CLI) and
Gemini (Gemini CLI). No API keys. Each engine measures its own answers, so it never falls back to another platform.
"""
import re

from app import ai

ENGINES = {"claude": "Claude", "chatgpt": "ChatGPT", "gemini": "Gemini"}
PLATFORM = {"claude": "claude", "chatgpt": "codex", "gemini": "gemini"}  # engine → app.ai platform
# Engines that older checks used (API keys, before AI visibility moved to subscriptions): names for their reports.
LABELS = {**ENGINES, "perplexity": "Perplexity", "grok": "Grok"}
URL_RE = re.compile(r"https?://[^\s)\]>\"']+")
SEARCH_FIRST = ("Search the web before answering, and list the source URLs you used before the final JSON line. "
                "Give each source's own website address, not a search-engine redirect link.\n\n")
# Google grounding/redirect links hide the real site, so "is your website cited?" can't match them.
REDIRECT_RE = re.compile(r"^https?://(?:[a-z0-9-]+\.)*(?:vertexaisearch\.cloud\.google\.com|google\.[a-z.]+/url\?)", re.I)


def available() -> list[str]:
    """Engines whose platform is installed, switched on, not paused and not known to be signed out."""
    return [e for e, p in PLATFORM.items()
            if ai.candidates(web=True, only=p) and ai.PLATFORMS[p].signed_in() is not False]


def _real_sources(urls: list[str]) -> list[str]:
    return [u for u in urls if not REDIRECT_RE.match(u)]


def ask(engine: str, question: str) -> dict:
    # Sources go before the JSON line the question ends with, so the JSON stays the last line.
    text = ai.ask(SEARCH_FIRST + question, web=True, only=PLATFORM[engine], purpose="aivis")
    return {"text": text, "sources": _real_sources(URL_RE.findall(text))}
