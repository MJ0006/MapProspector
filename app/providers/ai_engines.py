"""Answer engines for the AI visibility checker (A6).

Each engine gets the same local question and returns {"text": answer, "sources": [urls]}.
Claude runs through your local `claude` CLI (subscription). The others need API keys in .env;
model names are configurable because vendors rename them often:
  OPENAI_MODEL (default gpt-4.1-mini), PERPLEXITY_MODEL (sonar),
  GEMINI_MODEL (gemini-2.5-flash), XAI_MODEL (grok-4)
"""
import re

import httpx

from app import ai, settings

ENGINES = {
    "claude": "Claude",
    "chatgpt": "ChatGPT",
    "perplexity": "Perplexity",
    "gemini": "Gemini",
    "grok": "Grok",
}
_ENABLED_BY = {"claude": "claude", "chatgpt": "openai", "perplexity": "perplexity", "gemini": "gemini", "grok": "grok"}
URL_RE = re.compile(r"https?://[^\s)\]>\"']+")


def available() -> list[str]:
    mods = settings.modules()
    return [e for e, mod in _ENABLED_BY.items() if mods.get(mod)]


def ask(engine: str, question: str, client: httpx.Client | None = None) -> dict:
    fn = {"claude": _claude, "chatgpt": _openai, "perplexity": _perplexity, "gemini": _gemini, "grok": _grok}[engine]
    http = client or httpx.Client(timeout=120)
    try:
        return fn(question, http)
    finally:
        if client is None:
            http.close()


def _claude(q: str, _http) -> dict:
    # only="claude": this engine measures Claude's own answers, so it never falls back to another platform.
    # Sources go before the JSON line the question ends with, so the JSON stays the last line.
    text = ai.ask("Search the web before answering, and list the source URLs you used before the final JSON line.\n\n"
                  + q, web=True,
                  only="claude", purpose="aivis")
    return {"text": text, "sources": URL_RE.findall(text)}


def _openai(q: str, http: httpx.Client) -> dict:
    return _responses("https://api.openai.com/v1/responses", settings.env("OPENAI_API_KEY"),
                      settings.env("OPENAI_MODEL", "gpt-4.1-mini"), q, http)


def _responses(url: str, key: str, model: str, q: str, http: httpx.Client) -> dict:
    """OpenAI-style Responses API with its web_search tool (OpenAI and xAI)."""
    r = http.post(url, headers={"Authorization": f"Bearer {key}"},
                  json={"model": model, "input": q, "tools": [{"type": "web_search"}]})
    r.raise_for_status()
    data = r.json()
    text, sources = "", []
    for item in data.get("output", []):
        for c in item.get("content", []) or []:
            if c.get("type") == "output_text":
                text += c.get("text", "")
                sources += [a.get("url") for a in c.get("annotations", []) if a.get("url")]
    return {"text": text, "sources": sources or URL_RE.findall(text)}


def _perplexity(q: str, http: httpx.Client) -> dict:
    r = http.post("https://api.perplexity.ai/chat/completions",
                  headers={"Authorization": f"Bearer {settings.env('PERPLEXITY_API_KEY')}"},
                  json={"model": settings.env("PERPLEXITY_MODEL", "sonar"),
                        "messages": [{"role": "user", "content": q}]})
    r.raise_for_status()
    data = r.json()
    text = data["choices"][0]["message"]["content"]
    sources = data.get("citations") or [s.get("url") for s in data.get("search_results", []) if s.get("url")]
    return {"text": text, "sources": sources}


def _gemini(q: str, http: httpx.Client) -> dict:
    model = settings.env("GEMINI_MODEL", "gemini-2.5-flash")
    # Key in a header, never the URL: error messages include the URL and end up in reports you share.
    r = http.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                  headers={"x-goog-api-key": settings.env("GEMINI_API_KEY")},
                  json={"contents": [{"parts": [{"text": q}]}], "tools": [{"google_search": {}}]})
    r.raise_for_status()
    cand = (r.json().get("candidates") or [{}])[0]
    text = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", []))
    chunks = (cand.get("groundingMetadata") or {}).get("groundingChunks", [])
    # Grounding URIs are Google redirect links; the real site is in the chunk title (e.g. "acmeplumbing.com").
    sources = []
    for c in chunks:
        web = c.get("web") or {}
        uri, title = web.get("uri", ""), (web.get("title") or "").strip()
        if "vertexaisearch" in uri and "." in title and " " not in title:
            sources.append("https://" + title)
        elif uri and "vertexaisearch" not in uri:
            sources.append(uri)
    return {"text": text, "sources": sources}


def _grok(q: str, http: httpx.Client) -> dict:
    # xAI retired Live Search (search_parameters, now HTTP 410); its Responses API has a web_search tool.
    return _responses("https://api.x.ai/v1/responses", settings.env("XAI_API_KEY"), settings.env("XAI_MODEL", "grok-4"),
                      q, http)
