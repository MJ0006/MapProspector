"""AI drafting through the local `claude` CLI (your Claude Code login, no API key).

`claude -p` runs one headless turn and prints the answer. Everything here returns
drafts for the user to approve; nothing generated is ever sent or published directly.
"""
import json
from pathlib import Path
import subprocess
import sys
import time

from app import settings

TIMEOUT = 240


class AIUnavailable(RuntimeError):
    pass


def available() -> bool:
    return settings.claude_exe() is not None


def ask(prompt: str, *, web: bool = False, timeout: int = TIMEOUT, tools: list[str] | None = None,
        mcp: bool = False, purpose: str = "ai") -> str:
    """Run one prompt. web=True lets Claude use web search/fetch (AI visibility, citation checks).
    mcp=True connects the MapProspector MCP server (.mcp.json) so Claude can read your data;
    `tools` lists the only tools Claude may use without asking."""
    exe = settings.claude_exe()
    if not exe:
        raise AIUnavailable("The `claude` CLI is not installed or not on PATH.")
    # The prompt goes in on stdin: Windows' "claude.cmd" shim passes arguments through cmd.exe, which cuts
    # them at the first newline and at 8191 characters.
    cmd = [exe, "-p", "--output-format", "text"]
    allowed = list(tools or []) + (["WebSearch", "WebFetch"] if web else [])
    if mcp:
        cmd += ["--mcp-config", str(mcp_config()), "--strict-mcp-config"]
    if allowed:
        cmd += ["--allowedTools", ",".join(allowed)]
    cwd = settings.ROOT if mcp else (settings.DATA_DIR if settings.DATA_DIR.exists() else None)
    from app import journal
    t0 = time.monotonic()
    meta = {"purpose": purpose, "web": web, "mcp": mcp, "prompt_chars": len(prompt), "prompt_head": prompt[:300]}
    try:
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, cwd=cwd)
    except subprocess.TimeoutExpired:
        journal.record(purpose, "ai_call", "ask", "failed", detail=f"timed out after {timeout}s",
                       data={**meta, "ms": int((time.monotonic() - t0) * 1000)})
        raise AIUnavailable(f"Claude did not answer within {timeout} seconds.")
    ms = int((time.monotonic() - t0) * 1000)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "claude exited with an error").strip()[:500]
        journal.record(purpose, "ai_call", "ask", "failed", detail=err, data={**meta, "ms": ms})
        raise AIUnavailable(err)
    journal.record(purpose, "ai_call", "ask", data={**meta, "ms": ms, "reply_chars": len(proc.stdout)})
    return proc.stdout.strip()


def mcp_config() -> Path:
    """MCP config that starts the server with this same Python (the repo's .mcp.json says `python3`,
    which a venv that isn't activated, or the desktop app's bundled Python, wouldn't match)."""
    settings.ensure_dirs()
    path = settings.DATA_DIR / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {"mapprospector": {
        "command": sys.executable, "args": ["-m", "app.mcp_server"]}}}))
    return path


def ask_json(prompt: str, *, web: bool = False, timeout: int = TIMEOUT, purpose: str = "ai"):
    """Ask for JSON and parse the first JSON value in the reply."""
    text = ask(prompt + "\n\nReply with JSON only, no prose, no code fences.", web=web, timeout=timeout,
               purpose=purpose)
    try:
        return extract_json(text)
    except ValueError:
        from app import journal
        journal.failure(purpose, "parse_json", "Claude's reply had no valid JSON", data={"reply": text[:1000]})
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
