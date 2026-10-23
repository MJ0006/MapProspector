"""One-way copy of the SQLite database into PostgreSQL (POSTGRES_URL), for querying it with your own tools.

SQLite stays the app's database; Postgres only ever receives a copy. Each copy replaces the tables in the
`mapprospector` schema inside one transaction, so readers never see a half-finished copy. Edits made in
Postgres are overwritten by the next copy. Google sign-in tokens (oauth_tokens) are never copied.
"""
import time

from app import db, jobs, journal, settings

SCHEMA = "mapprospector"
SKIP = {"oauth_tokens"}
EVERY_SECONDS = 3600


def configured() -> bool:
    return bool(settings.env("POSTGRES_URL"))


def _pg_type(sqlite_type: str) -> str:
    t = (sqlite_type or "").upper()
    if "INT" in t:
        return "BIGINT"
    if any(x in t for x in ("REAL", "FLOA", "DOUB")):
        return "DOUBLE PRECISION"
    if "BLOB" in t:
        return "BYTEA"
    return "TEXT"


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def tables() -> list[dict]:
    """[{'name', 'columns': [(name, pg_type)], 'pk': [names]}] for every app table in SQLite."""
    out = []
    for t in db.all("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
        if t["name"] in SKIP:
            continue
        info = db.all(f"PRAGMA table_info({_q(t['name'])})")
        out.append({"name": t["name"], "columns": [(c["name"], _pg_type(c["type"])) for c in info],
                    "pk": [c["name"] for c in sorted(info, key=lambda c: c["pk"]) if c["pk"]]})
    return out


def _value(v, pg_type: str):
    if v is None or pg_type != "TEXT":
        return v
    return v if isinstance(v, str) else str(v)  # SQLite lets any value into a TEXT column


def copy(url: str | None = None) -> dict:
    """Copy every table; returns {'tables': n, 'rows': n}. Raises on failure (nothing is changed in Postgres then)."""
    import psycopg
    url = url or settings.env("POSTGRES_URL")
    if not url:
        raise RuntimeError("Set POSTGRES_URL in Settings → API keys first.")
    plan = tables()
    rows = 0
    with psycopg.connect(url, connect_timeout=10) as pg, pg.cursor() as cur:  # one transaction, committed on exit
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
        for t in plan:
            name = f"{SCHEMA}.{_q(t['name'])}"
            cols = ", ".join(f"{_q(c)} {typ}" for c, typ in t["columns"])
            pk = f", PRIMARY KEY ({', '.join(_q(c) for c in t['pk'])})" if t["pk"] else ""
            cur.execute(f"DROP TABLE IF EXISTS {name}")
            cur.execute(f"CREATE TABLE {name} ({cols}{pk})")
            names = [c for c, _ in t["columns"]]
            types = [typ for _, typ in t["columns"]]
            with cur.copy(f"COPY {name} ({', '.join(_q(c) for c in names)}) FROM STDIN") as cp:
                for r in db.all(f"SELECT * FROM {_q(t['name'])}"):
                    cp.write_row([_value(r[c], typ) for c, typ in zip(names, types)])
                    rows += 1
    return {"tables": len(plan), "rows": rows}


def start() -> None:
    db.set_setting("pg_copy_started", str(time.time()))
    jobs.submit(run)


def run() -> None:
    """Background copy that records its result for the Settings page and the Journal."""
    try:
        result = copy()
    except Exception as exc:
        db.set_setting("pg_copy_error", f"{type(exc).__name__}: {exc}"[:500])
        journal.failure("postgres", "copy", f"{type(exc).__name__}: {exc}")
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
