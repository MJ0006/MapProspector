"""SQLite access. Schema lives in schema.sql (all CREATE ... IF NOT EXISTS, safe to re-run)."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from app import settings

SCHEMA = Path(__file__).with_name("schema.sql")
_db_path: Path | None = None


# Columns added after the first release: (table, column, type). Applied to older databases on startup;
# new databases already get them from schema.sql. Add new entries at the end and bump nothing else.
COLUMNS = [
    ("companies", "gbp_location", "TEXT"), ("companies", "gsc_site", "TEXT"), ("companies", "lock_checked_at", "TEXT"),
    ("audits", "share_token", "TEXT"), ("citation_audits", "share_token", "TEXT"),
    ("scan_schedules", "last_error", "TEXT"), ("reports", "last_error", "TEXT"), ("tasks", "done_at", "TEXT"),
    ("photos", "error", "TEXT"),
]
UNIQUE_INDEXES = [("audits", "share_token"), ("citation_audits", "share_token")]


def init(path: Path | None = None) -> None:
    global _db_path
    settings.ensure_dirs()
    _db_path = path or settings.DB_PATH
    with connect() as c:
        c.execute("PRAGMA journal_mode = WAL")  # background jobs + MCP server write concurrently
        c.executescript(SCHEMA.read_text(encoding="utf-8"))
        migrate(c)


def migrate(c: sqlite3.Connection) -> None:
    for table, column, ctype in COLUMNS:
        cols = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
        if cols and column not in cols:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ctype}")
    for table, column in UNIQUE_INDEXES:
        c.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS ux_{table}_{column} ON {table}({column})")


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(_db_path or settings.DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.execute("PRAGMA busy_timeout = 30000")
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


def claim(table: str, row_id: int, from_statuses: tuple[str, ...], to: str) -> bool:
    """Atomically move a row's status; False if another worker already took it."""
    marks = ", ".join("?" for _ in from_statuses)
    with connect() as c:
        cur = c.execute(f"UPDATE {table} SET status = ? WHERE id = ? AND status IN ({marks})", (to, row_id, *from_statuses))
        return cur.rowcount == 1


def to_utc(value: str | None) -> str | None:
    """'2026-10-19T09:00' typed in a browser (local time) or any ISO string -> UTC ISO."""
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.astimezone()  # interpret as this computer's local time
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


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
