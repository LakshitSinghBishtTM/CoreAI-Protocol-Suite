"""SQLite-backed long-term memory. Scoped by (owner, namespace); nothing is shared
across owners. Values are PII-redacted on write unless ``redact=False``."""

import sqlite3
import time
from typing import Callable, Optional

from guardrails.pii import redact as pii_redact

MAX_VALUE_CHARS = 8_000
MAX_ITEMS_PER_SCOPE = 1_000


class MemoryStore:
    def __init__(
        self,
        path: str = ":memory:",
        *,
        retention_days: float = 0,  # 0 = keep until forgotten
        redact: bool = True,
        clock: Callable[[], float] = time.time,
    ):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.retention_s = retention_days * 86400
        self.redact, self.clock = redact, clock
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS memory (owner TEXT, ns TEXT, key TEXT, value TEXT,"
            " created REAL, updated REAL, PRIMARY KEY (owner, ns, key))"
        )

    def remember(self, owner: str, key: str, value: str, ns: str = "default") -> None:
        if not owner or not key:
            raise ValueError("owner and key are required")
        if len(value) > MAX_VALUE_CHARS:
            raise ValueError(f"value exceeds {MAX_VALUE_CHARS} characters")
        if self.redact:
            value = pii_redact(value)
        now = self.clock()
        self.expire()
        exists = self.db.execute(
            "SELECT 1 FROM memory WHERE owner=? AND ns=? AND key=?", (owner, ns, key)
        ).fetchone()
        if not exists:
            n = self.db.execute(
                "SELECT COUNT(*) FROM memory WHERE owner=? AND ns=?", (owner, ns)
            ).fetchone()[0]
            if n >= MAX_ITEMS_PER_SCOPE:  # bounded: evict the oldest-updated item
                self.db.execute(
                    "DELETE FROM memory WHERE rowid = (SELECT rowid FROM memory WHERE owner=? AND ns=?"
                    " ORDER BY updated LIMIT 1)",
                    (owner, ns),
                )
        self.db.execute(
            "INSERT INTO memory VALUES (?,?,?,?,?,?) ON CONFLICT(owner, ns, key) DO UPDATE SET"
            " value=excluded.value, updated=excluded.updated",
            (owner, ns, key, value, now, now),
        )
        self.db.commit()

    def recall(self, owner: str, key: str, ns: str = "default") -> Optional[str]:
        self.expire()
        row = self.db.execute(
            "SELECT value FROM memory WHERE owner=? AND ns=? AND key=?",
            (owner, ns, key),
        ).fetchone()
        return row[0] if row else None

    def view(self, owner: str, ns: Optional[str] = None) -> list[dict]:
        """Everything stored for ``owner`` (so people can see what is kept)."""
        self.expire()
        q = "SELECT ns, key, value, created, updated FROM memory WHERE owner=?"
        args: list = [owner]
        if ns is not None:
            q += " AND ns=?"
            args.append(ns)
        rows = self.db.execute(q + " ORDER BY ns, key", args).fetchall()
        return [
            dict(zip(("ns", "key", "value", "created", "updated"), r)) for r in rows
        ]

    def search(
        self, owner: str, text: str, ns: Optional[str] = None, limit: int = 5
    ) -> list[dict]:
        words = [w for w in text.lower().split() if len(w) > 2]
        scored = []
        for item in self.view(owner, ns):
            hay = f"{item['key']} {item['value']}".lower()
            s = sum(w in hay for w in words)
            if s:
                scored.append((s, item))
        scored.sort(key=lambda x: (-x[0], -x[1]["updated"]))
        return [i for _, i in scored[:limit]]

    def forget(
        self, owner: str, key: Optional[str] = None, ns: Optional[str] = None
    ) -> int:
        """Delete one key, one namespace, or (no key, no ns) everything for the owner."""
        q, args = "DELETE FROM memory WHERE owner=?", [owner]
        if ns is not None:
            q, args = q + " AND ns=?", args + [ns]
        if key is not None:
            q, args = q + " AND key=?", args + [key]
        n = self.db.execute(q, args).rowcount
        self.db.commit()
        return n

    def expire(self) -> int:
        if not self.retention_s:
            return 0
        n = self.db.execute(
            "DELETE FROM memory WHERE updated < ?", (self.clock() - self.retention_s,)
        ).rowcount
        self.db.commit()
        return n
