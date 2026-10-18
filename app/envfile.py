"""Read and update the .env file in place (Settings → API keys).

Only the KEY=value lines you change are rewritten; comments, order and keys this app doesn't know stay as they are.
"""
import os
import re
import tempfile
from pathlib import Path

from dotenv import dotenv_values

LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def read(path: Path) -> dict[str, str]:
    """Parsed the same way the server loads it at startup."""
    if not path.exists():
        return {}
    return {k: v or "" for k, v in dotenv_values(path, interpolate=False, encoding="utf-8").items()}


def _quote(value: str) -> str:
    """Quote values that .env would otherwise cut at a space or '#' (the server reads .env without expanding '$')."""
    if not re.search(r"[\s#$'\"\\]", value):
        return value
    if "'" not in value:
        return f"'{value}'"  # single quotes: taken literally
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def update(path: Path, changes: dict[str, str | None]) -> None:
    """Set keys (value) or remove them (None), so the app's default applies again.

    Every line for a key is changed (a duplicate further down would otherwise win). Written atomically so a crash
    can't leave half a file."""
    for key, value in changes.items():
        if value is not None and ("\n" in value or "\r" in value):
            raise ValueError(f"{key}: a value can't contain a line break")
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    out, written = [], set()
    for line in lines:
        m = LINE.match(line)
        if m and m.group(1) in changes:
            key, value = m.group(1), changes[m.group(1)]
            if value is None or key in written:
                continue
            line = f"{key}={_quote(value)}"
            written.add(key)
        out.append(line)
    lines = out + [f"{k}={_quote(v)}" for k, v in changes.items() if v is not None and k not in written]
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".env-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines) + "\n")
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)
