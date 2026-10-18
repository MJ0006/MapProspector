"""SQLite access. Schema lives in schema.sql (all CREATE ... IF NOT EXISTS, safe to re-run)."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from app import settings

SCHEMA = Path(__file__).with_name("schema.sql")
_db_path: Path | None = None


def init(path: Path | None = None) -> None:
    global _db_path
    settings.ensure_dirs()
    _db_path = path or settings.DB_PATH
    with connect() as c:
        c.executescript(SCHEMA.read_text())


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(_db_path or settings.DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


@contextmanager
def connect():
    c = _conn()
    try:
        yield c
        c.commit()
    finally:
        c.close()


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def all(sql: str, *args) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def one(sql: str, *args) -> dict | None:
    with connect() as c:
        r = c.execute(sql, args).fetchone()
        return dict(r) if r else None


def run(sql: str, *args) -> int:
    """Execute a write; returns lastrowid."""
    with connect() as c:
        return c.execute(sql, args).lastrowid


def insert(table: str, **cols) -> int:
    cols = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in cols.items()}
    names = ", ".join(cols)
    marks = ", ".join("?" for _ in cols)
    return run(f"INSERT INTO {table} ({names}) VALUES ({marks})", *cols.values())


def update(table: str, row_id: int, **cols) -> None:
    if not cols:
        return
    cols = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in cols.items()}
    sets = ", ".join(f"{k} = ?" for k in cols)
    run(f"UPDATE {table} SET {sets} WHERE id = ?", *cols.values(), row_id)


def loads(value, default=None):
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def get_setting(key: str, default: str = "") -> str:
    r = one("SELECT value FROM app_settings WHERE key = ?", key)
    return r["value"] if r else default


def set_setting(key: str, value: str) -> None:
    run("INSERT INTO app_settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value", key, value)
