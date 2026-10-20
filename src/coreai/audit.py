"""Audit log: who changed what, and when.

Records administrative and configuration events (kill switch, policy reloads,
budget config, later key changes). Kept in a bounded in-memory ring for the
API, and optionally appended to a JSONL file that rotates at ``max_bytes``
(one backup, ``<path>.1``), so it can never grow without limit.

Detail values under keys that look like secrets are redacted before storage.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

_SECRET_HINTS = ("key", "token", "secret", "password", "authorization", "credential")
_MAX_VALUE_CHARS = 500


def _clean(value: Any, depth: int = 0) -> Any:
    if isinstance(value, dict) and depth < 3:
        return {
            str(k): (
                "[redacted]"
                if any(h in str(k).lower() for h in _SECRET_HINTS)
                else _clean(v, depth + 1)
            )
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)) and depth < 3:
        return [_clean(v, depth + 1) for v in value[:50]]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    text = str(value)
    return text if len(text) <= _MAX_VALUE_CHARS else text[:_MAX_VALUE_CHARS] + "..."


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
                "action": action,
                "actor": actor,
                "target": target,
                "outcome": outcome,
                "details": _clean(details or {}),
            }
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
