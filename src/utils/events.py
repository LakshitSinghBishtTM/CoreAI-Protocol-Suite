"""Event bus for outbound notifications: run completion, budget alerts, provider outages.

Sinks are callables taking one event dict (sync or async). Emitting never blocks
or raises: delivery is fire-and-forget on the running loop, failures are logged.

Env configuration (``configure_from_env``):
  CAIPS_EVENT_WEBHOOK_URL, CAIPS_EVENT_WEBHOOK_SECRET   signed webhook (both required)
  CAIPS_EVENT_WEBHOOK_EVENTS                            optional comma list, e.g. run.finished,budget.exceeded

Webhook signing matches the approval webhook:
``X-CaiPS-Signature = HMAC-SHA256(secret, "<timestamp>.<body>")``, timestamp in ``X-CaiPS-Timestamp``.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import json
import os
import time
import uuid
from typing import Any, Callable, Iterable, Optional

import httpx
from loguru import logger

EVENT_TYPES = (
    "run.finished",
    "budget.warning",
    "budget.exceeded",
    "provider.outage",
    "provider.recovered",
)


def sign(secret: str, timestamp: str, body: str) -> str:
    return hmac.new(secret.encode(), f"{timestamp}.{body}".encode(), hashlib.sha256).hexdigest()


class WebhookSink:
    def __init__(self, url: str, secret: str, events: Optional[Iterable[str]] = None, transport=None):
        if not secret:
            raise ValueError("event webhook needs a signing secret")
        self.url, self.secret, self.transport = url, secret, transport
        self.events = set(events) if events else None

    async def __call__(self, event: dict) -> None:
        if self.events is not None and event["type"] not in self.events:
            return
        body = json.dumps(event, sort_keys=True, default=str)
        ts = str(int(time.time()))
        headers = {
            "Content-Type": "application/json",
            "X-CaiPS-Timestamp": ts,
            "X-CaiPS-Signature": sign(self.secret, ts, body),
        }
        async with httpx.AsyncClient(timeout=5, transport=self.transport) as http:
            (await http.post(self.url, content=body, headers=headers)).raise_for_status()


class EventBus:
    def __init__(self) -> None:
        self.sinks: list[Callable[[dict], Any]] = []
        self.tasks: set[asyncio.Task] = set()

    def subscribe(self, sink: Callable[[dict], Any]) -> None:
        self.sinks.append(sink)

    def clear(self) -> None:
        self.sinks.clear()

    def emit(self, type_: str, **data: Any) -> Optional[dict]:
        if not self.sinks:
            return None
        event = {"id": uuid.uuid4().hex, "type": type_, "ts": time.time(), **data}
        for sink in list(self.sinks):
            self._deliver(sink, event)
        return event

    def _deliver(self, sink: Callable[[dict], Any], event: dict) -> None:
        try:
            out = sink(event)
            if not inspect.isawaitable(out):
                return
            task = asyncio.get_running_loop().create_task(self._guard(out, sink))
        except RuntimeError:  # no running loop: drop rather than block
            if inspect.iscoroutine(out):
                out.close()
            return
        except Exception as exc:
            logger.warning(f"event sink {sink!r} failed: {exc}")
            return
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    @staticmethod
    async def _guard(awaitable: Any, sink: Any) -> None:
        try:
            await awaitable
        except Exception as exc:
            logger.warning(f"event sink {sink!r} failed: {exc}")

    def configure_from_env(self) -> None:
        url = os.getenv("CAIPS_EVENT_WEBHOOK_URL", "").strip()
        if not url:
            return
        secret = os.getenv("CAIPS_EVENT_WEBHOOK_SECRET", "")
        if not secret:
            logger.warning("CAIPS_EVENT_WEBHOOK_URL set without CAIPS_EVENT_WEBHOOK_SECRET; webhook disabled")
            return
        wanted = [e.strip() for e in os.getenv("CAIPS_EVENT_WEBHOOK_EVENTS", "").split(",") if e.strip()]
        unknown = [e for e in wanted if e not in EVENT_TYPES]
        if unknown:
            logger.warning(f"unknown event types in CAIPS_EVENT_WEBHOOK_EVENTS: {unknown}")
        self.subscribe(WebhookSink(url, secret, wanted or None))


def watch_budgets(manager: Any, emit: Callable[..., Any]) -> None:
    """Emit ``budget.warning`` / ``budget.exceeded`` once per window per subject.

    Wraps ``record`` and ``add_usage`` on *manager* and reads its public ``status()``,
    so ``coreai.budgets`` itself needs no changes. ``emit(type, **data)`` is usually ``bus.emit``.
    """
    seen: dict[str, tuple[float, set]] = {}

    def check(subject: str) -> None:
        try:
            st = manager.status(subject)
            if not st:
                return
            cost, cap = st["used"]["cost_usd"], st["limits"]["cost_usd"]
            last, fired = seen.get(subject, (0.0, set()))
            if cost < last:  # spend went down: a new window started
                fired = set()
            kinds = []
            if st["warning"] and "warning" not in fired:
                kinds.append("warning")
            if cap is not None and cost >= cap and "exceeded" not in fired:
                kinds.append("exceeded")
            for kind in kinds:
                fired.add(kind)
                emit(f"budget.{kind}", subject=subject, used_usd=cost, cap_usd=cap)
            seen[subject] = (cost, fired)
        except Exception as exc:  # alerts must never break request accounting
            logger.warning(f"budget alert check failed: {exc}")

    for name in ("record", "add_usage"):
        orig = getattr(manager, name)

        def wrapped(subject, *a, _orig=orig, **kw):
            result = _orig(subject, *a, **kw)
            check(subject)
            return result

        setattr(manager, name, wrapped)


bus = EventBus()
