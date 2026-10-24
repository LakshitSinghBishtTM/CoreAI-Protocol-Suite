"""Audit log: who changed what, and when.

Records administrative and configuration events (kill switch, policy reloads,
budget config, later key changes). Kept in a bounded in-memory ring for the
API, and optionally appended to a JSONL file that rotates at ``max_bytes``
(one backup, ``<path>.1``), so it can never grow without limit.

Detail values under keys that look like secrets are redacted before storage.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

_SECRET_HINTS = ("key", "token", "secret", "password", "authorization", "credential")
_MAX_VALUE_CHARS = 500
_MAX_CONTAINER_ITEMS = 50
_MAX_CLEAN_CHARS = 50_000
_MAX_EVENT_BYTES = 64_000


def _clean(value: Any, depth: int = 0, budget: list[int] | None = None) -> Any:
    if budget is None:
        budget = [_MAX_CLEAN_CHARS]
    if budget[0] <= 0:
        return "[truncated]"
    if isinstance(value, dict):
        if depth >= 3:
            return "[nested data truncated]"
        out = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= _MAX_CONTAINER_ITEMS or budget[0] <= 0:
                out["_truncated"] = True
                break
            raw_key = str(key)
            safe_key = _clip_text(raw_key, budget)
            if any(h in raw_key.lower() for h in _SECRET_HINTS):
                out[safe_key] = "[redacted]"
                budget[0] = max(0, budget[0] - len("[redacted]"))
            else:
                out[safe_key] = _clean(item, depth + 1, budget)
        return out
    if isinstance(value, (list, tuple)):
        if depth >= 3:
            return "[nested data truncated]"
        out = [_clean(v, depth + 1, budget) for v in value[:_MAX_CONTAINER_ITEMS]]
        if len(value) > _MAX_CONTAINER_ITEMS and budget[0] > 0:
            out.append("[truncated]")
        return out
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return value if value.bit_length() <= 1024 else "[number truncated]"
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    text = str(value)
    return _clip_text(text, budget)


def _clip_text(text: str, budget: list[int]) -> str:
    size = min(_MAX_VALUE_CHARS, budget[0])
    if len(text) <= size:
        clipped = text
    else:
        marker = "..." if size >= 3 else ""
        clipped = text[: size - len(marker)] + marker
    budget[0] -= len(clipped)
    return clipped


class AuditLog:
    def __init__(
        self,
        path: str | None = None,
        max_events: int = 1000,
        max_bytes: int = 5_000_000,
        clock: Callable[[], float] = time.time,
    ):
        self.path = path
        self._max_bytes = max_bytes
        self._clock = clock
        self._events: deque[dict] = deque(maxlen=max_events)
        self._lock = threading.Lock()
        self._seq = 0
        self.write_errors = 0

    def record(
        self,
        action: str,
        actor: str,
        target: str | None = None,
        outcome: str = "ok",
        details: dict | None = None,
    ) -> dict:
        with self._lock:
            self._seq += 1
            event = {
                "id": self._seq,
                "ts": round(self._clock(), 3),
                "action": _clean(action),
                "actor": _clean(actor),
                "target": _clean(target),
                "outcome": _clean(outcome),
                "details": _clean(details or {}),
            }
            if (
                len(json.dumps(event, separators=(",", ":")).encode("utf-8"))
                > _MAX_EVENT_BYTES
            ):
                event["details"] = {"_truncated": "event exceeded size limit"}
            self._events.append(event)
            self._append_to_file(event)
        return event

    def _append_to_file(self, event: dict) -> None:
        if not self.path:
            return
        try:
            if (
                os.path.exists(self.path)
                and os.path.getsize(self.path) >= self._max_bytes
            ):
                os.replace(self.path, self.path + ".1")
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, separators=(",", ":")) + "\n")
        except OSError:
            # Auditing must never take the gateway down; the ring still has it.
            self.write_errors += 1

    def events(
        self,
        limit: int = 100,
        action: str | None = None,
        actor: str | None = None,
    ) -> list[dict]:
        """Newest first. *action* matches exactly or as a ``prefix.``."""
        with self._lock:
            items = list(self._events)
        out = []
        for e in reversed(items):
            if action and not (
                e["action"] == action or e["action"].startswith(action + ".")
            ):
                continue
            if actor and e["actor"] != actor:
                continue
            out.append(e)
            if len(out) >= limit:
                break
        return out
