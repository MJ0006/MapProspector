"""Claude through Claude Code (`claude -p`), signed in with your Claude subscription."""
from app import settings
from app.ai.mcp import server as mcp_server
from app.ai.base import Platform, run_cli


class Claude(Platform):
    name = "claude"
    label = "Claude"
    web = True
    mcp = True
    install = "Install Claude Code (claude.com/claude-code), then sign in."
    signin = ["claude"]  # the first run asks you to log in

    def exe(self) -> str | None:
        return settings.claude_exe()

    def run(self, prompt, *, web, mcp, tools, timeout) -> str:
        cmd = [self.exe(), "-p", "--output-format", "text"]
        allowed = list(tools) + (["WebSearch", "WebFetch"] if web else [])
        if mcp:
            cmd += ["--mcp-config", str(_mcp_config()), "--strict-mcp-config"]
        if allowed:
            cmd += ["--allowedTools", ",".join(allowed)]
        cwd = settings.ROOT if mcp else (settings.DATA_DIR if settings.DATA_DIR.exists() else None)
        return run_cli(self.label, cmd, prompt, timeout, cwd=cwd)


def _mcp_config():
    import json
    settings.ensure_dirs()
    path = settings.DATA_DIR / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {"mapprospector": mcp_server()}}), encoding="utf-8")
    return path
