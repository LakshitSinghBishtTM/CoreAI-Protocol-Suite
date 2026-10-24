"""Phase 6: web search, read-only SQL, allow-listed shell."""

import sqlite3

import httpx
import pytest

from tools import ApprovalRequired, ToolError, ToolRegistry
from tools.extra import (
    MAX_RESULT_CHARS,
    ShellRunner,
    SqliteReader,
    WebSearch,
    _format_rows,
    register_extras,
)


def search(payload, **kw):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=payload)

    return WebSearch("https://s.example/search", params={"format": "json"},
                     transport=httpx.MockTransport(handler), **kw), seen  # fmt: skip


async def test_search_formats_and_caps_results():
    items = [
        {"title": f"T{i}\nline", "url": f"https://x/{i}", "content": "c" * 1000}
        for i in range(9)
    ]
    ws, seen = search({"results": items})
    out = await ws.search("cats", count=99)  # capped by max_results
    assert out.count("https://x/") == 5 and "\nline" not in out.replace("\n  ", "")
    assert max(len(line) for line in out.splitlines()) <= 410
    assert seen[0].url.params["q"] == "cats" and seen[0].url.params["format"] == "json"


async def test_search_failures_are_tool_errors():
    with pytest.raises(ToolError):
        await search({"nope": []})[0].search("x")
    ws = WebSearch(
        "https://s/", transport=httpx.MockTransport(lambda r: httpx.Response(500))
    )
    with pytest.raises(ToolError):
        await ws.search("x")
    nested, _ = search(
        {"data": {"hits": [{"title": "a", "link": "https://l"}]}},
        results_path="data.hits",
    )
    assert "https://l" in await nested.search("x")


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "t.db"
    con = sqlite3.connect(path)
    con.execute("create table t(a int, b text)")
    con.executemany("insert into t values (?,?)", [(i, f"v{i}") for i in range(300)])
    con.commit()
    con.close()
    return SqliteReader({"main": path}), path


def test_sql_select_works_and_caps_rows(db):
    reader, _ = db
    out = reader.query("main", "select a, b from t where a < 3 order by a;")
    assert out.splitlines() == ["a\tb", "0\tv0", "1\tv1", "2\tv2"]
    assert "add LIMIT" in reader.query("main", "select * from t")


def test_sqlite_query_caps_wide_result_before_returning(db):
    reader, _ = db
    query = "SELECT " + ", ".join(f"'{('x' * 500)}' AS c{i}" for i in range(60))
    out = reader.query("main", query)
    assert len(out) <= MAX_RESULT_CHARS
    assert out.endswith("[output truncated]")


def test_sql_result_formatting_caps_wide_results():
    out = _format_rows([f"column_{i}" for i in range(500)], [["x" * 500] * 500])
    assert len(out) <= MAX_RESULT_CHARS
    assert out.endswith("[output truncated]")


@pytest.mark.parametrize(
    "sql",
    [
        "delete from t",
        "drop table t",
        "update t set a=1",
        "insert into t values (1,'x')",
        "select 1; drop table t",
        "pragma writable_schema=1",
        "attach database 'x' as y",
        "with x as (select 1) delete from t",
    ],
)
def test_sql_writes_are_refused_and_data_survives(db, sql):
    reader, path = db
    with pytest.raises(ToolError):
        reader.query("main", sql)
    con = sqlite3.connect(path)
    assert con.execute("select count(*) from t").fetchone()[0] == 300


def test_sql_unknown_database_and_runaway_query(db):
    reader, _ = db
    with pytest.raises(ToolError):
        reader.query("other", "select 1")
    with pytest.raises(
        ToolError
    ):  # cartesian explosion is cut off by the progress handler
        reader.query("main", "select count(*) from t a, t b, t c, t d, t e")


async def test_shell_allow_list_no_shell_features_clean_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRET_KEY_FOR_TEST", "leak")
    sh = ShellRunner(["echo", "env", "sleep", "cat"], tmp_path, timeout_s=1)
    out = await sh.run("echo hello world")
    assert out.startswith("exit code 0") and "hello world" in out
    assert "SECRET_KEY_FOR_TEST" not in await sh.run("env")
    assert "$(whoami)" in await sh.run(
        "echo '$(whoami)'"
    )  # printed literally, not executed
    piped = await sh.run("echo a | cat")  # the pipe is just an argument, not a pipe
    assert "a | cat" in piped
    with pytest.raises(ToolError) as e:
        await sh.run("rm -rf /")
    assert e.value.code == "not_allowed"
    with pytest.raises(ToolError) as e:
        await sh.run("sleep 5")
    assert e.value.code == "timeout"


async def test_registration_is_opt_in_and_shell_needs_approval(tmp_path):
    r = ToolRegistry()
    register_extras(r)
    assert r.visible(["*"]) == []
    ws, _ = search({"results": []})
    register_extras(
        r,
        ws,
        SqliteReader({"main": tmp_path / "x.db"}),
        ShellRunner(["echo"], tmp_path),
    )
    assert [t.name for t in r.visible(["*"])] == ["db_query", "shell_run", "web_search"]
    with pytest.raises(ApprovalRequired):
        await r.call("shell_run", {"command": "echo hi"}, allowed=["*"])
    ok = await r.call("shell_run", {"command": "echo hi"}, allowed=["*"], approved=True)
    assert "hi" in ok.content
    with pytest.raises(ToolError) as e:
        await r.call("db_query", {"database": "nope", "sql": "select 1"}, allowed=["*"])
    assert e.value.code == "invalid_arguments"  # schema enum rejects unknown databases
