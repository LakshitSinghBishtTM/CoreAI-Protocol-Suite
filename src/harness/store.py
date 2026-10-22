"""harness/store.py: durable run storage (sqlite) with checkpoints and a trace.

One transaction per checkpoint: run row + full message history together, so a
crash leaves either the previous step or the new one, never half of each.
"""

import contextlib
import json
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict
from typing import Optional

from providers.base import Message

ACTIVE = ("running", "paused", "waiting_approval")
SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(
  id TEXT PRIMARY KEY, agent TEXT, owner TEXT, status TEXT, stop_reason TEXT,
  step INTEGER DEFAULT 0, cost_usd DOUBLE PRECISION DEFAULT 0, input_tokens INTEGER DEFAULT 0,
  output_tokens INTEGER DEFAULT 0, control TEXT DEFAULT '', pending TEXT,
  messages TEXT, result TEXT, created DOUBLE PRECISION, updated DOUBLE PRECISION, deadline DOUBLE PRECISION);
CREATE INDEX IF NOT EXISTS runs_owner ON runs(owner, status);
CREATE TABLE IF NOT EXISTS events(
  run_id TEXT, seq INTEGER, ts DOUBLE PRECISION, type TEXT, data TEXT, PRIMARY KEY(run_id, seq));
"""


def dump(msgs: list[Message]) -> str:
    return json.dumps(
        [
            {k: v for k, v in asdict(m).items() if v is not None and k != "images"}
            for m in msgs
        ]
    )


def load(raw: str) -> list[Message]:
    return [Message(**d) for d in json.loads(raw or "[]")]


def _is_pg(path: str) -> bool:
    return str(path).startswith(("postgres://", "postgresql://"))


class RunStore:
    """SQLite by default; pass a ``postgresql://`` DSN (needs ``psycopg``) for Postgres."""

    def __init__(self, path: str = ":memory:"):
        self._lock = threading.RLock()
        self._pg = _is_pg(path)
        if self._pg:
            try:
                import psycopg
                from psycopg.rows import dict_row
            except ImportError as exc:  # pragma: no cover
                raise RuntimeError("Postgres run store needs the 'postgres' extra (psycopg).") from exc
            self._integrity = psycopg.errors.UniqueViolation
            self._db = psycopg.connect(path, autocommit=True, row_factory=dict_row)
            for stmt in filter(None, (x.strip() for x in SCHEMA.split(";"))):
                self._db.execute(stmt)
        else:
            self._integrity = sqlite3.IntegrityError
            self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
            self._db.row_factory = sqlite3.Row
            self._db.executescript(SCHEMA)

    def _x(self, sql, args=()):
        """Execute without taking the lock (callers hold it)."""
        return self._db.execute(sql.replace("?", "%s") if self._pg else sql, args)

    def _q(self, sql, args=()):
        with self._lock:
            return self._x(sql, args)

    @contextlib.contextmanager
    def _tx(self):
        with self._lock:
            if self._pg:
                with self._db.transaction():
                    yield
                return
            self._db.execute("BEGIN")
            try:
                yield
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    def create(
        self, agent: str, owner: str, messages: list[Message], deadline: Optional[float]
    ) -> str:
        rid, now = uuid.uuid4().hex[:16], time.time()
        self._q(
            "INSERT INTO runs(id,agent,owner,status,messages,created,updated,deadline) VALUES(?,?,?,?,?,?,?,?)",
            (rid, agent, owner, "running", dump(messages), now, now, deadline),
        )
        return rid

    def get(self, rid: str) -> Optional[dict]:
        r = self._q("SELECT * FROM runs WHERE id=?", (rid,)).fetchone()
        return dict(r) if r else None

    def messages(self, rid: str) -> list[Message]:
        r = self.get(rid)
        return load(r["messages"]) if r else []

    def checkpoint(self, rid: str, messages: list[Message], **fields) -> None:
        cols = {"messages": dump(messages), "updated": time.time(), **fields}
        if "pending" in cols and cols["pending"] is not None:
            cols["pending"] = json.dumps(cols["pending"])
        sets = ",".join(f"{k}=?" for k in cols)
        with self._tx():
            self._x(f"UPDATE runs SET {sets} WHERE id=?", (*cols.values(), rid))

    def set(self, rid: str, **fields) -> None:
        fields["updated"] = time.time()
        sets = ",".join(f"{k}=?" for k in fields)
        self._q(f"UPDATE runs SET {sets} WHERE id=?", (*fields.values(), rid))

    def request(self, rid: str, control: str) -> bool:
        """Ask a live run to cancel/pause. Only active runs accept it."""
        cur = self._q(
            "UPDATE runs SET control=? WHERE id=? AND status IN ('running','paused','waiting_approval')",
            (control, rid),
        )
        return cur.rowcount > 0

    def kill(self, owner: Optional[str] = None) -> int:
        """Kill switch: cancel every active run, for one owner or the whole instance."""
        q = "UPDATE runs SET control='kill' WHERE status IN ('running','paused','waiting_approval')"
        cur = self._q(q + (" AND owner=?" if owner else ""), (owner,) if owner else ())
        return cur.rowcount

    def list_runs(
        self, owner: Optional[str] = None, status: Optional[str] = None
    ) -> list[dict]:
        sql, args = (
            "SELECT id,agent,owner,status,stop_reason,step,cost_usd,created FROM runs WHERE 1=1",
            [],
        )
        for col, v in (("owner", owner), ("status", status)):
            if v:
                sql += f" AND {col}=?"
                args.append(v)
        return [
            dict(r) for r in self._q(sql + " ORDER BY created DESC", args).fetchall()
        ]

    def interrupted(self) -> list[str]:
        """Runs left 'running' by a crashed process; resume() picks them up."""
        return [
            r["id"]
            for r in self._q("SELECT id FROM runs WHERE status='running'").fetchall()
        ]

    # -- trace -----------------------------------------------------------
    def event(self, rid: str, type_: str, **data) -> int:
        with self._lock:
            for attempt in range(5):  # another process may take the same seq
                seq = self._x(
                    "SELECT COALESCE(MAX(seq),0)+1 AS n FROM events WHERE run_id=?", (rid,)
                ).fetchone()["n"]
                try:
                    self._x(
                        "INSERT INTO events VALUES(?,?,?,?,?)",
                        (rid, seq, time.time(), type_, json.dumps(data)),
                    )
                    return seq
                except self._integrity:
                    if attempt == 4:
                        raise
        raise AssertionError("unreachable")

    def events(self, rid: str, after: int = 0) -> list[dict]:
        rows = self._q(
            "SELECT seq,ts,type,data FROM events WHERE run_id=? AND seq>? ORDER BY seq",
            (rid, after),
        )
        return [
            {"seq": r["seq"], "ts": r["ts"], "type": r["type"], **json.loads(r["data"])}
            for r in rows
        ]


def diff_traces(a: list[dict], b: list[dict]) -> list[str]:
    """Behavioural diff of two runs: model/tool sequence, ignoring timing and ids."""

    def sig(e):
        return (
            e["type"],
            e.get("tool") or e.get("model"),
            e.get("ok"),
            e.get("reason"),
        )

    sa, sb = [sig(e) for e in a if e["type"] != "checkpoint"], [
        sig(e) for e in b if e["type"] != "checkpoint"
    ]
    out = []
    for i in range(max(len(sa), len(sb))):
        x, y = (sa[i] if i < len(sa) else None), (sb[i] if i < len(sb) else None)
        if x != y:
            out.append(f"event {i}: {x} != {y}")
    return out
