"""One-way copy of the SQLite database into PostgreSQL (POSTGRES_URL), for querying it with your own tools.

SQLite stays the app's database; Postgres only ever receives a copy. Each copy replaces the tables in the
`mapprospector` schema inside one transaction, so readers never see a half-finished copy. Edits made in
Postgres are overwritten by the next copy. Google sign-in tokens (oauth_tokens) are never copied.
"""
import time
from urllib.parse import urlparse

from app import db, jobs, journal, settings

SCHEMA = "mapprospector"
SKIP = {"oauth_tokens"}
EVERY_SECONDS = 3600


def configured() -> bool:
    return bool(settings.env("POSTGRES_URL"))


def _pg_type(declared: str, stored: set[str]) -> str:
    """Postgres type for a column. SQLite lets any value into any column, so the values actually stored decide:
    an INTEGER column holding 99.5 becomes DOUBLE PRECISION, one holding '' becomes TEXT."""
    t = (declared or "").upper()
    stored = stored - {"null"}
    numeric = "INT" in t or any(x in t for x in ("REAL", "FLOA", "DOUB"))
    if numeric and stored <= {"integer"} and "INT" in t:
        return "BIGINT"
    if numeric and stored <= {"integer", "real"}:
        return "DOUBLE PRECISION"
    if "BLOB" in t and stored <= {"blob"}:
        return "BYTEA"
    return "TEXT"


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def tables(c) -> list[dict]:
    """[{'name', 'columns': [(name, pg_type)], 'pk': [names]}] for every app table, read through SQLite connection c."""
    out = []
    for (name,) in c.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
        if name in SKIP:
            continue
        info = c.execute(f"PRAGMA table_info({_q(name)})").fetchall()
        cols = [(col["name"], _pg_type(col["type"], {r[0] for r in c.execute(
            f"SELECT DISTINCT typeof({_q(col['name'])}) FROM {_q(name)}")})) for col in info]
        out.append({"name": name, "columns": cols,
                    "pk": [col["name"] for col in sorted(info, key=lambda col: col["pk"]) if col["pk"]]})
    return out


def _value(v, pg_type: str):
    if v is None or pg_type != "TEXT":
        return v
    return v if isinstance(v, str) else str(v)


def copy(url: str | None = None) -> dict:
    """Copy every table; returns {'tables': n, 'rows': n}. Raises on failure (nothing is changed in Postgres then)."""
    import psycopg
    url = url or settings.env("POSTGRES_URL")
    if not url:
        raise RuntimeError("Set POSTGRES_URL in Settings → API keys first.")
    rows = 0
    # One SQLite read transaction (a WAL snapshot), so all tables are copied as of the same moment.
    with db.connect() as c, psycopg.connect(url, connect_timeout=10) as pg, pg.cursor() as cur:
        c.execute("BEGIN")
        plan = tables(c)
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")  # one Postgres transaction, committed on exit
        for t in plan:
            name = f"{SCHEMA}.{_q(t['name'])}"
            cols = ", ".join(f"{_q(col)} {typ}" for col, typ in t["columns"])
            pk = f", PRIMARY KEY ({', '.join(_q(col) for col in t['pk'])})" if t["pk"] else ""
            cur.execute(f"DROP TABLE IF EXISTS {name}")
            cur.execute(f"CREATE TABLE {name} ({cols}{pk})")
            names = [col for col, _ in t["columns"]]
            types = [typ for _, typ in t["columns"]]
            with cur.copy(f"COPY {name} ({', '.join(_q(col) for col in names)}) FROM STDIN") as cp:
                for r in c.execute(f"SELECT * FROM {_q(t['name'])}"):
                    cp.write_row([_value(r[col], typ) for col, typ in zip(names, types)])
                    rows += 1
    return {"tables": len(plan), "rows": rows}


def _redact(message: str) -> str:
    """Connection errors can echo the URL; keep its password out of Settings and the Journal."""
    url = settings.env("POSTGRES_URL") or ""
    password = urlparse(url).password if "://" in url else None
    for secret in filter(None, (url, password)):
        message = message.replace(secret, "••••")
    return message


def start() -> None:
    db.set_setting("pg_copy_started", str(time.time()))
    jobs.submit(run)


def run() -> None:
    """Background copy that records its result for the Settings page and the Journal."""
    try:
        result = copy()
    except Exception as exc:
        error = _redact(f"{type(exc).__name__}: {exc}")
        db.set_setting("pg_copy_error", error[:500])
        journal.failure("postgres", "copy", error)
        return
    db.set_setting("pg_copy_error", "")
    db.set_setting("pg_copy_at", db.now())
    db.set_setting("pg_copy_result", f"{result['tables']} tables, {result['rows']} rows")


@jobs.on_tick
def hourly() -> None:
    if not configured():
        return
    try:
        last = float(db.get_setting("pg_copy_started", "0"))
    except ValueError:
        last = 0
    if time.time() - last >= EVERY_SECONDS:
        start()
