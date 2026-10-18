"""How every platform starts the MapProspector MCP server: this same Python, read-only tools only."""
import sys

READONLY_ENV = {"MAPPROSPECTOR_MCP_READONLY": "1"}


def server() -> dict:
    # The repo's .mcp.json says `python3`, which an unactivated venv or the Windows app's bundled Python wouldn't match.
    return {"command": sys.executable, "args": ["-m", "app.mcp_server"], "env": dict(READONLY_ENV)}
