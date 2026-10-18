"""How every platform starts the MapProspector MCP server: this same Python, read-only tools only."""
import sys

from app import settings

READONLY_ENV = {"MAPPROSPECTOR_MCP_READONLY": "1"}


def server() -> dict:
    # The repo's .mcp.json says `python3`, which an unactivated venv or the Windows app's bundled Python wouldn't match.
    # DATA_DIR / MAPPROSPECTOR_ENV_FILE are passed on explicitly: some CLIs give MCP servers only a minimal environment,
    # and without them the server would open an empty database next to the app instead of yours.
    env = {**READONLY_ENV, "DATA_DIR": str(settings.DATA_DIR), "MAPPROSPECTOR_ENV_FILE": str(settings.ENV_FILE)}
    return {"command": sys.executable, "args": ["-m", "app.mcp_server"], "env": env}
