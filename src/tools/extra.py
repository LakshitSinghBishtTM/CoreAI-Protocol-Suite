"""tools/extra.py

More built-in tools, all opt-in through config and all restrictive:

- ``web_search``: queries one configured JSON search endpoint (for example a
  SearXNG instance). The endpoint is admin config, not model input.
- ``db_query``: read-only SQL against configured SQLite files. Only a single
  SELECT is accepted, the file is opened read-only, rows and size are capped.
- ``shell_run``: runs a program from an explicit allow list. No shell is
  involved (so no pipes, globbing or substitution), the environment is empty
  apart from PATH, and the call is DESTRUCTIVE so it needs human approval.
"""

import asyncio
import os
import shlex
import sqlite3
from pathlib import Path
from typing import Any, Optional

import httpx

from .registry import Permission, Tool, ToolError, ToolRegistry

MAX_ROWS = 200
MAX_CELL_CHARS = 500


class WebSearch:
    def __init__(
        self,
        url: str,
        query_param: str = "q",
        params: Optional[dict] = None,
        headers: Optional[dict] = None,
        results_path: str = "results",
        max_results: int = 5,
        transport=None,
    ):
        self.url, self.query_param = url, query_param
        self.params, self.headers = params or {}, headers or {}
        self.results_path, self.max_results = results_path, max_results
        self.transport = transport

    async def search(self, query: str, count: int = 5) -> str:
        count = max(1, min(int(count), self.max_results))
        try:
            async with httpx.AsyncClient(timeout=10, transport=self.transport) as http:
                r = await http.get(
                    self.url,
                    params={**self.params, self.query_param: query},
                    headers=self.headers,
                )
                r.raise_for_status()
                data: Any = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ToolError(
                "tool_failed", f"Search failed: {type(exc).__name__}"
            ) from exc
        for key in filter(None, self.results_path.split(".")):
            data = data.get(key) if isinstance(data, dict) else None
        if not isinstance(data, list):
            raise ToolError("tool_failed", "Search response had no results list.")
        lines = []
        for item in data[:count]:
            if not isinstance(item, dict):
                continue
            title = " ".join(str(item.get("title", "")).split())[:200]
            url = str(item.get("url") or item.get("link") or "")[:500]
            snippet = " ".join(
                str(item.get("content") or item.get("snippet") or "").split()
            )[:400]
            lines.append(f"- {title}\n  {url}\n  {snippet}")
        return "\n".join(lines) or "No results."


class SqliteReader:
    def __init__(self, databases: dict[str, str | Path]):
        self.databases = {k: Path(v).resolve() for k, v in databases.items()}

    def query(self, database: str, sql: str) -> str:
        path = self.databases.get(database)
        if path is None:
            raise ToolError("invalid_arguments", f"Unknown database '{database}'.")
        stmt = _single_select(sql)
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            conn.execute("PRAGMA query_only = ON")

            def deny(action, *_):  # belt and braces on top of read-only mode
                allowed = (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
                           sqlite3.SQLITE_RECURSIVE)  # fmt: skip
                return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY

            conn.set_authorizer(deny)
            # Stop runaway queries.
            ticks = {"n": 0}

            def progress():
                ticks["n"] += 1
                return 1 if ticks["n"] > 20_000 else 0

            conn.set_progress_handler(progress, 1000)
            cur = conn.execute(stmt)
            cols = [d[0] for d in cur.description or []]
            rows = cur.fetchmany(MAX_ROWS + 1)
        except sqlite3.Error as exc:
            raise ToolError("tool_failed", f"SQL error: {exc}") from exc
        finally:
            conn.close()
        more = len(rows) > MAX_ROWS
        rows = rows[:MAX_ROWS]
        out = ["\t".join(cols)]
        out += ["\t".join(str(c)[:MAX_CELL_CHARS] for c in row) for row in rows]
        if more:
            out.append(f"[more than {MAX_ROWS} rows; add LIMIT]")
        return "\n".join(out)


def _single_select(sql: str) -> str:
    stmt = sql.strip().rstrip(";").strip()
    if ";" in stmt or not stmt.lower().startswith(("select", "with")):
        raise ToolError("invalid_arguments", "Only a single SELECT statement is allowed.")
    return stmt


def _format_rows(cols: list[str], rows: list) -> str:
    more = len(rows) > MAX_ROWS
    out = ["\t".join(cols)]
    out += ["\t".join(str(c)[:MAX_CELL_CHARS] for c in row) for row in rows[:MAX_ROWS]]
    if more:
        out.append(f"[more than {MAX_ROWS} rows; add LIMIT]")
    return "\n".join(out)


class PostgresReader:
    """Read-only SQL against Postgres: read-only transaction, statement timeout, row cap."""

    def __init__(self, dsns: dict[str, str], statement_timeout_ms: int = 10_000):
        self.dsns = {k: os.path.expandvars(v) for k, v in dsns.items()}
        self.statement_timeout_ms = statement_timeout_ms

    async def query(self, database: str, sql: str) -> str:
        dsn = self.dsns.get(database)
        if dsn is None:
            raise ToolError("invalid_arguments", f"Unknown database '{database}'.")
        stmt = _single_select(sql)
        try:
            import asyncpg
        except ImportError as exc:
            raise ToolError("tool_failed", "Postgres needs the 'postgres' extra (asyncpg).") from exc
        try:
            conn = await asyncpg.connect(
                dsn, timeout=5,
                server_settings={
                    "statement_timeout": str(self.statement_timeout_ms),
                    "default_transaction_read_only": "on",
                },
            )  # fmt: skip
            try:
                async with conn.transaction(readonly=True):
                    prepared = await conn.prepare(stmt)
                    cols = [a.name for a in prepared.get_attributes()]
                    cursor = await prepared.cursor()
                    rows = [tuple(r.values()) for r in await cursor.fetch(MAX_ROWS + 1)]
            finally:
                await conn.close()
        except (asyncpg.PostgresError, OSError, asyncio.TimeoutError) as exc:
            raise ToolError("tool_failed", f"SQL error: {exc}") from exc
        return _format_rows(cols, rows)


def _is_postgres(value: str | Path) -> bool:
    return str(value).startswith(("postgres://", "postgresql://"))


class DatabaseReader:
    """Routes ``db_query`` to SQLite files or Postgres DSNs by the configured value."""

    def __init__(self, databases: dict[str, str | Path]):
        pg = {k: str(v) for k, v in databases.items() if _is_postgres(v)}
        self.sqlite = SqliteReader({k: v for k, v in databases.items() if k not in pg})
        self.pg = PostgresReader(pg)
        self.databases = {**self.sqlite.databases, **self.pg.dsns}

    async def query(self, database: str, sql: str) -> str:
        if database in self.pg.dsns:
            return await self.pg.query(database, sql)
        return await asyncio.to_thread(self.sqlite.query, database, sql)


class ShellRunner:
    def __init__(
        self, allowed_commands: list[str], cwd: str | Path, timeout_s: float = 20.0
    ):
        self.allowed = set(allowed_commands)
        self.cwd = Path(cwd).resolve()
        self.timeout_s = timeout_s

    async def run(self, command: str) -> str:
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            raise ToolError("invalid_arguments", f"Bad command line: {exc}") from exc
        if not argv or argv[0] not in self.allowed:
            raise ToolError(
                "not_allowed",
                f"Command '{argv[0] if argv else ''}' is not allow-listed.",
            )
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=self.cwd, env={"PATH": os.environ.get("PATH", "")},
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )  # fmt: skip
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), self.timeout_s)
        except asyncio.TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise ToolError(
                "timeout", f"Command timed out after {self.timeout_s}s."
            ) from exc
        text = out.decode(errors="replace")
        return f"exit code {proc.returncode}\n{text}"


def register_extras(
    registry: ToolRegistry,
    search: Optional[WebSearch] = None,
    db: Optional[SqliteReader] = None,
    shell: Optional[ShellRunner] = None,
) -> None:
    def obj(props: dict, req: list[str]) -> dict:
        return {
            "type": "object",
            "properties": props,
            "required": req,
            "additionalProperties": False,
        }

    if search:
        registry.register(Tool(
            "web_search", "Search the web and return titles, URLs and snippets.",
            obj({"query": {"type": "string", "maxLength": 300},
                 "count": {"type": "integer", "minimum": 1, "maximum": 10}}, ["query"]),
            search.search, timeout_s=15,
        ))  # fmt: skip
    if db:
        registry.register(Tool(
            "db_query", "Run one read-only SELECT against a configured database.",
            obj({"database": {"type": "string", "enum": sorted(db.databases)},
                 "sql": {"type": "string", "maxLength": 2000}}, ["database", "sql"]),
            db.query, timeout_s=10,
        ))  # fmt: skip
    if shell:
        registry.register(Tool(
            "shell_run", "Run an allow-listed program (no shell features). Needs approval.",
            obj({"command": {"type": "string", "maxLength": 1000}}, ["command"]),
            shell.run, timeout_s=shell.timeout_s + 5, permission=Permission.DESTRUCTIVE,
        ))  # fmt: skip
