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
    text = ai.ask(q + "\n\nSearch the web before answering. List the source URLs you used at the end.", web=True,
                  only="claude", purpose="aivis")
    return {"text": text, "sources": URL_RE.findall(text)}


def _openai(q: str, http: httpx.Client) -> dict:
    r = http.post("https://api.openai.com/v1/responses",
                  headers={"Authorization": f"Bearer {settings.env('OPENAI_API_KEY')}"},
                  json={"model": settings.env("OPENAI_MODEL", "gpt-4.1-mini"), "input": q,
                        "tools": [{"type": "web_search"}]})
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
    r = http.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                  params={"key": settings.env("GEMINI_API_KEY")},
                  json={"contents": [{"parts": [{"text": q}]}], "tools": [{"google_search": {}}]})
    r.raise_for_status()
    cand = (r.json().get("candidates") or [{}])[0]
    text = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", []))
    chunks = (cand.get("groundingMetadata") or {}).get("groundingChunks", [])
    return {"text": text, "sources": [c["web"]["uri"] for c in chunks if c.get("web", {}).get("uri")]}


def _grok(q: str, http: httpx.Client) -> dict:
    r = http.post("https://api.x.ai/v1/chat/completions",
                  headers={"Authorization": f"Bearer {settings.env('XAI_API_KEY')}"},
                  json={"model": settings.env("XAI_MODEL", "grok-4"), "messages": [{"role": "user", "content": q}],
                        "search_parameters": {"mode": "auto", "return_citations": True}})
    r.raise_for_status()
    data = r.json()
    text = data["choices"][0]["message"]["content"]
    return {"text": text, "sources": data.get("citations") or URL_RE.findall(text)}
