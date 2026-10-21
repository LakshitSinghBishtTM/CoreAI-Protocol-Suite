"""User feedback (thumbs up/down, corrections) stored against request ids.

Nothing is stored beyond what the caller submits: the gateway never saves
prompts or responses on its own. A caller may attach the ``prompt`` and a
``correction``; feedback with both can be exported as eval tasks.
"""

from __future__ import annotations

import builtins  # noqa: F401  (the `list` method shadows the builtin)
import json
import sqlite3
import time
from typing import Callable, Optional

MAX_TEXT = 8_000
MAX_ROWS_PER_OWNER = 10_000


class FeedbackStore:
    def __init__(self, path: str = ":memory:", clock: Callable[[], float] = time.time):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.clock = clock
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS feedback (id INTEGER PRIMARY KEY, owner TEXT,"
            " request_id TEXT, rating INTEGER, comment TEXT, prompt TEXT,"
            " correction TEXT, model TEXT, created REAL,"
            " UNIQUE(owner, request_id))"
        )

    def record(
        self,
        owner: str,
        request_id: str,
        rating: int,
        *,
        comment: str = "",
        prompt: str = "",
        correction: str = "",
        model: str = "",
    ) -> None:
        """One feedback row per (owner, request); resubmitting replaces it."""
        if not owner or not request_id:
            raise ValueError("owner and request_id are required")
        if rating not in (-1, 1):
            raise ValueError("rating must be 1 (up) or -1 (down)")
        for name, v in (
            ("comment", comment),
            ("prompt", prompt),
            ("correction", correction),
        ):
            if len(v) > MAX_TEXT:
                raise ValueError(f"{name} exceeds {MAX_TEXT} characters")
        (n,) = self.db.execute(
            "SELECT COUNT(*) FROM feedback WHERE owner=? AND request_id!=?",
            (owner, request_id),
        ).fetchone()
        if n >= MAX_ROWS_PER_OWNER:
            raise ValueError("feedback limit reached; delete old feedback first")
        self.db.execute(
            "INSERT OR REPLACE INTO feedback (owner, request_id, rating, comment, prompt,"
            " correction, model, created) VALUES (?,?,?,?,?,?,?,?)",
            (
                owner,
                request_id,
                rating,
                comment,
                prompt,
                correction,
                model,
                self.clock(),
            ),
        )
        self.db.commit()

    def list(
        self, owner: str, rating: Optional[int] = None, limit: int = 100
    ) -> list[dict]:
        q = "SELECT request_id, rating, comment, prompt, correction, model, created FROM feedback WHERE owner=?"
        args: "builtins.list" = [owner]
        if rating is not None:
            q, args = q + " AND rating=?", args + [rating]
        rows = self.db.execute(
            q + " ORDER BY created DESC LIMIT ?", args + [limit]
        ).fetchall()
        cols = (
            "request_id",
            "rating",
            "comment",
            "prompt",
            "correction",
            "model",
            "created",
        )
        return [dict(zip(cols, r)) for r in rows]

    def summary(self, owner: str) -> dict:
        up, down = (
            self.db.execute(
                "SELECT COUNT(*) FROM feedback WHERE owner=? AND rating=?", (owner, r)
            ).fetchone()[0]
            for r in (1, -1)
        )
        return {"up": up, "down": down}

    def delete(self, owner: str, request_id: str) -> int:
        n = self.db.execute(
            "DELETE FROM feedback WHERE owner=? AND request_id=?", (owner, request_id)
        ).rowcount
        self.db.commit()
        return n

    def eval_tasks(self, owner: str) -> list[dict]:
        """Corrections that carry a prompt, as ``exact`` eval tasks (see docs/evals.md)."""
        return [
            {
                "id": f"feedback-{r['request_id']}",
                "prompt": r["prompt"],
                "scorer": {"type": "exact", "expected": r["correction"]},
                "tags": ["feedback"],
            }
            for r in self.list(owner, limit=MAX_ROWS_PER_OWNER)
            if r["prompt"] and r["correction"]
        ]

    def export_json(self, owner: str) -> str:
        return json.dumps({"tasks": self.eval_tasks(owner)}, indent=2)
