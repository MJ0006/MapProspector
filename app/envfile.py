"""Read and update the .env file in place (Settings → API keys).

Only the KEY=value lines you change are rewritten; comments, order and keys this app doesn't know stay as they are.
"""
import os
import re
import tempfile
from pathlib import Path

LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def read(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            m = LINE.match(line)
            if m:
                out[m.group(1)] = _unquote(m.group(2))
    return out


def update(path: Path, changes: dict[str, str | None]) -> None:
    """Set keys (value) or remove their value (None). Written atomically so a crash can't leave half a file."""
    for key, value in changes.items():
        if value is not None and ("\n" in value or "\r" in value):
            raise ValueError(f"{key}: a value can't contain a line break")
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    pending = dict(changes)
    for i, line in enumerate(lines):
        m = LINE.match(line)
        if m and m.group(1) in pending:
            value = pending.pop(m.group(1))
            lines[i] = f"{m.group(1)}={value or ''}"
    lines += [f"{k}={v}" for k, v in pending.items() if v is not None]
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".env-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines) + "\n")
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)
