"""ChatGPT through OpenAI's Codex CLI (`codex exec`), signed in with your ChatGPT plan (`codex login`)."""
import json
import subprocess
import time
import uuid

from app import settings
from app.ai.base import Platform, run_cli
from app.ai.mcp import server as mcp_server

_status_cache: dict = {}


def _toml(value) -> str:
    return json.dumps(value)  # JSON strings, lists and inline objects of strings are valid TOML values


class Codex(Platform):
    name = "codex"
    label = "ChatGPT"
    web = True
    mcp = True
    install = "npm install -g @openai/codex, then sign in with your ChatGPT account."
    signin = ["codex", "login"]

    def exe(self) -> str | None:
        return settings.find_cli("codex", "CODEX_PATH")

    def signed_in(self) -> bool | None:
        exe = self.exe()
        if not exe:
            return False
        hit = _status_cache.get(exe)
        if hit and time.monotonic() - hit[0] < 60:
            return hit[1]
        try:
            proc = subprocess.run([exe, "login", "status"], capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=15, stdin=subprocess.DEVNULL)
            out = (proc.stdout or "") + (proc.stderr or "")
            ok = proc.returncode == 0 and "Logged in" in out and "Not logged in" not in out
        except (OSError, subprocess.TimeoutExpired):
            _status_cache[exe] = (time.monotonic(), None)  # don't make every Settings load wait 15 s again
            return None
        _status_cache[exe] = (time.monotonic(), ok)
        return ok

    def run(self, prompt, *, web, mcp, tools, timeout) -> str:
        settings.ensure_dirs()
        out_file = settings.DATA_DIR / f"codex-reply-{uuid.uuid4().hex}.txt"
        # Read-only sandbox, never asks for approval, keeps no session files.
        cmd = [self.exe(), "exec", "--skip-git-repo-check", "--ephemeral", "-s", "read-only", "--color", "never",
               "-c", 'approval_policy="never"', "-o", str(out_file)]
        if web:
            cmd += ["-c", 'web_search="live"']
        if mcp:
            srv = mcp_server()
            cmd += ["-c", f"mcp_servers.mapprospector.command={_toml(srv['command'])}",
                    "-c", f"mcp_servers.mapprospector.args={_toml(srv['args'])}",
                    "-c", "mcp_servers.mapprospector.env={" + ", ".join(f"{k}={_toml(v)}" for k, v in srv["env"].items()) + "}"]
        cmd.append("-")  # prompt from stdin
        try:
            out = run_cli(self.label, cmd, prompt, timeout, cwd=settings.ROOT if mcp else settings.DATA_DIR)
            if out_file.exists():
                out = out_file.read_text(encoding="utf-8").strip() or out
            return out
        finally:
            out_file.unlink(missing_ok=True)
