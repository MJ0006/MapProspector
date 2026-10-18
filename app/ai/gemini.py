"""Gemini through Google's Gemini CLI (headless), signed in with "Login with Google"."""
import json
import os
from pathlib import Path

from app import settings
from app.ai.base import Platform, PlatformError, classify, retry_time, run_cli
from app.ai.mcp import server as mcp_server


class Gemini(Platform):
    name = "gemini"
    label = "Gemini"
    web = True  # built-in Google Search tool
    mcp = True
    install = "npm install -g @google/gemini-cli, then sign in with Google."
    signin = ["gemini"]  # the first run asks how to sign in: choose "Login with Google"

    def exe(self) -> str | None:
        return settings.find_cli("gemini", "GEMINI_PATH")

    def signed_in(self) -> bool | None:
        if not self.installed():
            return False
        home = Path(os.environ.get("GEMINI_CLI_HOME") or Path.home())
        return (home / ".gemini" / "oauth_creds.json").is_file() or bool(os.environ.get("GEMINI_API_KEY")) or None

    def run(self, prompt, *, web, mcp, tools, timeout) -> str:
        # Runs in its own folder whose .gemini/settings.json lists only the MapProspector server (read-only tools).
        work = settings.DATA_DIR / "ai" / "gemini"
        (work / ".gemini").mkdir(parents=True, exist_ok=True)
        config: dict = {}
        if mcp:
            from app.mcp_server import READ_ONLY
            config["mcpServers"] = {"mapprospector": {**mcp_server(), "cwd": str(settings.ROOT), "trust": True,
                                                      "includeTools": list(READ_ONLY)}}
        (work / ".gemini" / "settings.json").write_text(json.dumps(config), encoding="utf-8")
        # No -p: the prompt comes on stdin, which also makes the CLI non-interactive.
        cmd = [self.exe(), "-o", "json", "--skip-trust"]
        if mcp:
            cmd += ["--allowed-mcp-server-names", "mapprospector"]
        try:
            out = run_cli(self.label, cmd, prompt, timeout, cwd=work)
        except PlatformError as exc:
            raise _from_json(str(exc)) or exc
        try:
            data = json.loads(out)
        except ValueError:
            return out
        if data.get("error"):
            raise _from_json(out) or PlatformError(str(data["error"]))
        return str(data.get("response") or "").strip()


def _from_json(text: str) -> PlatformError | None:
    """Gemini prints {"error": {"message", "code"}}; code 41 means not signed in."""
    start = text.find("{")
    try:
        err = json.loads(text[start:]).get("error") if start >= 0 else None
    except ValueError:
        return None
    if not isinstance(err, dict):
        return None
    msg = str(err.get("message") or err)
    kind = "auth" if err.get("code") == 41 else classify(msg)
    return PlatformError(msg, kind, retry_time(msg))
