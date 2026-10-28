"""Gemini through Google's Antigravity CLI (`agy`, headless), signed in with your Google account.

Gemini CLI stopped serving personal Google accounts on 2026-06-18; Antigravity CLI replaces it.
"""
import json
import os
from pathlib import Path

from app import settings
from app.ai.base import Platform, PlatformError, classify, retry_time, run_cli
from app.ai.mcp import server as mcp_server

MCP_RULE = "mcp(mapprospector/*)"  # the server only shows read-only tools (enabledTools below)


class Gemini(Platform):
    name = "gemini"
    label = "Gemini"
    web = True  # built-in Google Search
    mcp = True
    install = ("Install Antigravity CLI (Windows PowerShell: irm https://antigravity.google/cli/install.ps1 | iex; "
               "macOS/Linux: curl -fsSL https://antigravity.google/cli/install.sh | bash), then sign in with Google.")
    signin = ["agy"]  # the first run asks how to sign in: choose Google OAuth

    def exe(self) -> str | None:
        found = settings.find_cli("agy", "AGY_PATH")
        if found or settings.env("AGY_PATH"):
            return found
        local = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "agy" / "bin" / "agy.exe"  # Windows installer
        return str(local) if local.is_file() else None

    def signed_in(self) -> bool | None:
        # Sign-in lives in the system keyring, so only a real call can tell.
        return None if self.installed() else False

    def run(self, prompt, *, web, mcp, tools, timeout) -> str:
        # Runs in its own folder whose .agents/mcp_config.json lists only the MapProspector server (read-only tools).
        work = settings.DATA_DIR / "ai" / "antigravity"
        (work / ".agents").mkdir(parents=True, exist_ok=True)
        config: dict = {"mcpServers": {}}
        if mcp:
            from app.mcp_server import READ_ONLY
            config["mcpServers"]["mapprospector"] = {**mcp_server(), "cwd": str(settings.ROOT),
                                                     "enabledTools": list(READ_ONLY)}
            _allow_mcp()
        (work / ".agents" / "mcp_config.json").write_text(json.dumps(config), encoding="utf-8")
        # The prompt goes on stdin as one stream-json "user" event; the CLI exits once stdin closes and it has answered.
        cmd = [self.exe(), "--input-format", "stream-json", "--output-format", "stream-json"]
        event = json.dumps({"event": "user", "message": {"content": prompt}}) + "\n"
        try:
            out = run_cli(self.label, cmd, event, timeout, cwd=work)
        except PlatformError as exc:
            raise _from_text(str(exc)) or exc
        result = None
        for line in out.splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if isinstance(ev, dict) and ev.get("event") == "result":
                result = ev.get("result") or {}
        if result is None:
            raise PlatformError(f"{self.label} gave no result: {out[-300:]}")
        if result.get("status") != "SUCCESS":
            msg = str(result.get("error") or f"{self.label} run ended with status {result.get('status')}")
            raise _from_text(msg) or PlatformError(msg, classify(msg), retry_time(msg))
        return str(result.get("response") or "").strip()


def _from_text(text: str) -> PlatformError | None:
    """A headless run that isn't signed in exits with "authentication required"."""
    if "authentication required" in text.lower():
        return PlatformError(text, "auth")
    return None


def _allow_mcp() -> None:
    """Headless runs can't ask, so MCP tools need an allow rule in the CLI's settings; add ours once."""
    path = Path.home() / ".gemini" / "antigravity-cli" / "settings.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, ValueError):
        return  # never overwrite a settings file we can't read
    if not isinstance(data, dict):
        return
    perms = data.setdefault("permissions", {})
    allow = perms.setdefault("allow", []) if isinstance(perms, dict) else None
    if not isinstance(allow, list) or MCP_RULE in allow:
        return
    allow.append(MCP_RULE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
