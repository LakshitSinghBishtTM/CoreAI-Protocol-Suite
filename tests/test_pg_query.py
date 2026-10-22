import asyncio
import tempfile

import pytest

from tools.extra import DatabaseReader, PostgresReader
from tools.registry import ToolError

pgserver = pytest.importorskip("pgserver")
pytest.importorskip("asyncpg")


@pytest.fixture(scope="module")
def dsn():
    server = pgserver.get_server(tempfile.mkdtemp())
    server.psql("create table t(id int, name text); insert into t select g, 'n'||g from generate_series(1,300) g;")
    yield server.get_uri()
    server.cleanup()


def run(coro):
    return asyncio.run(coro)


def test_select_and_row_cap(dsn):
    r = PostgresReader({"main": dsn})
    out = run(r.query("main", "select id, name from t order by id")).splitlines()
    assert out[0] == "id\tname" and out[1] == "1\tn1"
    assert "more than 200 rows" in out[-1]


def test_empty_result_keeps_columns(dsn):
    assert run(PostgresReader({"m": dsn}).query("m", "select id from t where id < 0")) == "id"


@pytest.mark.parametrize(
    "sql",
    ["delete from t", "select 1; drop table t", "with d as (delete from t returning id) select * from d"],
)
def test_writes_rejected(dsn, sql):
    with pytest.raises(ToolError):
        run(PostgresReader({"m": dsn}).query("m", sql))
    assert run(PostgresReader({"m": dsn}).query("m", "select count(*) c from t")).splitlines()[1] == "300"


def test_statement_timeout(dsn):
    r = PostgresReader({"m": dsn}, statement_timeout_ms=200)
    with pytest.raises(ToolError, match="SQL error"):
        run(r.query("m", "select pg_sleep(2)"))


def test_unknown_database_and_router(dsn, tmp_path):
    import sqlite3

    f = tmp_path / "a.db"
    c = sqlite3.connect(f)
    c.execute("create table x(v int)")
    c.execute("insert into x values (7)")
    c.commit()
    c.close()
    d = DatabaseReader({"pg": dsn, "lite": f})
    assert set(d.databases) == {"pg", "lite"}
    assert run(d.query("lite", "select v from x")).splitlines() == ["v", "7"]
    assert run(d.query("pg", "select 1 as one")).splitlines() == ["one", "1"]
    with pytest.raises(ToolError):
        run(d.query("nope", "select 1"))
