"""One-way Postgres copy. The live copy test runs only when TEST_POSTGRES_URL points at a throwaway database."""
import os

import pytest

from app import db, pgcopy
from app.modules.crm import routes as crm


def test_plan_skips_tokens_and_maps_types():
    crm.create_contact(business_name="Joe's")
    db.run("UPDATE contacts SET is_spam = '' WHERE id = 1")  # SQLite allows text in an INTEGER column
    with db.connect() as c:
        plan = {t["name"]: t for t in pgcopy.tables(c)}
    assert "oauth_tokens" not in plan
    assert "contacts" in plan and plan["contacts"]["pk"] == ["id"]
    cols = dict(plan["contacts"]["columns"])
    assert cols["id"] == "BIGINT" and cols["email"] == "TEXT" and cols["is_spam"] == "TEXT"
    assert dict(plan["deals"]["columns"])["value"] == "DOUBLE PRECISION"


def test_error_never_shows_the_password(monkeypatch):
    monkeypatch.setenv("POSTGRES_URL", "postgresql://me:hunter2@127.0.0.1:1/x")
    monkeypatch.setattr(pgcopy, "copy", lambda: (_ for _ in ()).throw(ValueError("bad dsn postgresql://me:hunter2@127.0.0.1:1/x")))
    pgcopy.run()
    assert "hunter2" not in db.get_setting("pg_copy_error")
    assert "hunter2" not in db.one("SELECT detail FROM events WHERE area = 'postgres'")["detail"]


def test_failed_copy_is_shown_and_journaled(client, monkeypatch):
    monkeypatch.setenv("POSTGRES_URL", "postgresql://nobody@127.0.0.1:1/none")
    client.post("/settings/postgres/copy")
    assert db.get_setting("pg_copy_error")
    assert db.one("SELECT 1 FROM events WHERE area = 'postgres' AND kind = 'failure'")
    assert "Last copy failed" in client.get("/settings").text


@pytest.mark.skipif(not os.environ.get("TEST_POSTGRES_URL"), reason="needs TEST_POSTGRES_URL")
def test_copy_into_postgres(monkeypatch):
    import psycopg
    url = os.environ["TEST_POSTGRES_URL"]
    monkeypatch.setenv("POSTGRES_URL", url)
    crm.create_contact(business_name="Joe's", email="hello@joes.com", source="manual")
    db.run("INSERT INTO oauth_tokens (provider, data, updated_at) VALUES ('google', '{}', ?)", db.now())
    pgcopy.start()
    assert db.get_setting("pg_copy_error") == "" and db.get_setting("pg_copy_at")
    crm.create_contact(business_name="Ann's")
    db.run("UPDATE contacts SET is_spam = '' WHERE business_name = 'Ann''s'")  # mixed types still copy
    pgcopy.run()  # second copy replaces the first
    with psycopg.connect(url) as pg:
        names = [r[0] for r in pg.execute("SELECT business_name FROM mapprospector.contacts ORDER BY id")]
        assert names == ["Joe's", "Ann's"]
        assert not pg.execute("SELECT 1 FROM information_schema.tables WHERE table_schema = 'mapprospector' "
                              "AND table_name = 'oauth_tokens'").fetchone()
