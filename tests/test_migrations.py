"""Migrations run forward, backward and forward again on SQLite (always) and
Postgres (when TEST_POSTGRES_URL is set, as in CI)."""

import os

import pytest
from sqlalchemy import text

from database import db, migrations


def test_splitter_keeps_dollar_bodies_and_drops_transaction_words():
    sql = (
        "BEGIN;\n-- note; with semicolon\nCREATE TABLE a (x TEXT DEFAULT 'a;b');\n"
        "CREATE FUNCTION f() RETURNS int AS $$ BEGIN RETURN 1; END; $$ LANGUAGE plpgsql;\nCOMMIT;"
    )
    out = migrations.split_statements(sql)
    assert len(out) == 2
    assert "'a;b'" in out[0] and "RETURN 1; END;" in out[1]


async def _roundtrip():
    await migrations.migrate_up()
    async with db.get_session() as s:
        n = (await s.execute(text("SELECT count(*) FROM providers"))).scalar()
        assert n > 0  # seed rows from 001
        await s.execute(text("SELECT count(*) FROM api_requests"))
    await migrations.migrate_down(2)
    async with db.get_session() as s:
        with pytest.raises(Exception):
            await s.execute(text("SELECT 1 FROM providers"))
    await migrations.migrate_up()
    async with db.get_session() as s:
        assert (await s.execute(text("SELECT count(*) FROM providers"))).scalar() > 0


@pytest.fixture
async def fresh_db():
    def use(url):
        db.init_db(url)

    yield use
    await db.close_db()


async def test_sqlite_migrations_roundtrip(tmp_path, fresh_db):
    fresh_db(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    await _roundtrip()


@pytest.mark.skipif(
    not os.getenv("TEST_POSTGRES_URL"), reason="needs TEST_POSTGRES_URL"
)
async def test_postgres_migrations_roundtrip(fresh_db):
    fresh_db(os.environ["TEST_POSTGRES_URL"])
    await _roundtrip()
