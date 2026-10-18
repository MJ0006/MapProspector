"""AI drafting through the local `claude` CLI (your Claude Code login, no API key).

`claude -p` runs one headless turn and prints the answer. Everything here returns
drafts for the user to approve; nothing generated is ever sent or published directly.
"""
import json
import shutil
import subprocess

from app import settings

TIMEOUT = 240


class AIUnavailable(RuntimeError):
    pass


def available() -> bool:
    return shutil.which("claude") is not None


def ask(prompt: str, *, web: bool = False, timeout: int = TIMEOUT) -> str:
    """Run one prompt. web=True lets Claude use web search/fetch (AI visibility checks)."""
    if not available():
        raise AIUnavailable("The `claude` CLI is not installed or not on PATH.")
    cmd = ["claude", "-p", prompt, "--output-format", "text"]
    if web:
        cmd += ["--allowedTools", "WebSearch,WebFetch"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                          cwd=settings.DATA_DIR if settings.DATA_DIR.exists() else None)
    if proc.returncode != 0:
        raise AIUnavailable((proc.stderr or proc.stdout or "claude exited with an error").strip()[:500])
    return proc.stdout.strip()


def ask_json(prompt: str, *, web: bool = False, timeout: int = TIMEOUT):
    """Ask for JSON and parse the first JSON value in the reply."""
    text = ask(prompt + "\n\nReply with JSON only, no prose, no code fences.", web=web, timeout=timeout)
    return extract_json(text)


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
