"""harness/triggers.py: scheduled and event-triggered agent runs.

Four trigger kinds start ordinary harness runs (same limits, trace and
approvals as a manual run):

* ``every(seconds, ...)``: a fixed interval. ``tick(now)`` starts whatever is
  due, so tests drive it with a fake clock; ``run()`` is the real loop.
* ``cron(expr, ...)``: a five-field UTC cron expression (``harness/cron.py``).
* ``webhook(secret, ...)``: fires on a signed ``POST /v1/triggers/{name}/fire``
  (``X-CaiPS-Timestamp`` + ``X-CaiPS-Signature`` = HMAC-SHA256 of
  ``"<timestamp>.<body>"``, 5 minute window). JSON body fields fill the task.
* ``on_event(type, ...)``: fires when the event bus emits ``type`` (for
  example ``budget.exceeded`` or ``provider.outage``) and the optional
  ``match`` fields agree.

Safety rules, because unattended runs spend money:
* a trigger never overlaps itself: if its last run is still going, the firing
  is skipped and counted;
* each trigger has ``max_per_hour`` (default 6), so an event storm cannot
  start unbounded runs;
* event fields are substituted into the task as short plain text only.
"""

import asyncio
import hashlib
import hmac
import json
import string
import time
from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class Trigger:
    name: str
    agent: str
    task: str
    owner: str = "local"
    interval_s: float = 0.0  # 0 = event-only
    cron: Optional[object] = None  # harness.cron.Cron
    webhook_secret: str = ""  # non-empty enables POST /v1/triggers/{name}/fire
    event: str = ""
    match: dict = field(default_factory=dict)
    max_per_hour: int = 6
    next_at: float = 0.0
    fired: list = field(default_factory=list)  # timestamps within the last hour
    skipped: int = 0
    last_run: Optional[str] = None
    _task: Optional[asyncio.Task] = None


def _plain(v) -> str:
    return str(v).replace("\n", " ")[:200]


class TriggerManager:
    def __init__(self, runner, clock: Callable[[], float] = time.time):
        self.runner, self.clock = runner, clock
        self.triggers: dict[str, Trigger] = {}

    def every(self, name, seconds, agent, task, owner="local", max_per_hour=6):
        if seconds <= 0:
            raise ValueError("interval must be positive")
        return self._add(
            Trigger(name, agent, task, owner, interval_s=seconds,
                    max_per_hour=max_per_hour, next_at=self.clock() + seconds)
        )  # fmt: skip

    def cron(self, name, expr, agent, task, owner="local", max_per_hour=6):
        from .cron import Cron

        c = Cron(expr)
        return self._add(
            Trigger(name, agent, task, owner, cron=c, max_per_hour=max_per_hour,
                    next_at=c.next_after(self.clock()))
        )  # fmt: skip

    def webhook(self, name, secret, agent, task, owner="local", max_per_hour=6):
        if not secret:
            raise ValueError("webhook trigger needs a signing secret")
        return self._add(
            Trigger(name, agent, task, owner, webhook_secret=secret, max_per_hour=max_per_hour)
        )  # fmt: skip

    def fire_webhook(self, name: str, body: bytes, timestamp: str, signature: str) -> bool:
        """Verify and fire. Raises PermissionError on a bad or stale signature,
        KeyError for an unknown or non-webhook trigger. False means skipped (cap/overlap)."""
        t = self.triggers.get(name)
        if t is None or not t.webhook_secret:
            raise KeyError(name)
        try:
            fresh = abs(self.clock() - int(timestamp)) <= 300
        except (TypeError, ValueError):
            fresh = False
        good = hmac.new(
            t.webhook_secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256
        ).hexdigest()
        if not (fresh and hmac.compare_digest(good, signature or "")):
            raise PermissionError("bad or stale signature")
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            data = {}
        return self._fire(t, data if isinstance(data, dict) else {})

    def on_event(self, name, event, agent, task, owner="local", match=None, max_per_hour=6):
        return self._add(
            Trigger(name, agent, task, owner, event=event,
                    match=dict(match or {}), max_per_hour=max_per_hour)
        )  # fmt: skip

    def _add(self, t: Trigger) -> Trigger:
        if t.name in self.triggers:
            raise ValueError(f"trigger {t.name!r} already exists")
        self.runner._agent(t.agent)  # unknown agent fails at registration
        self.triggers[t.name] = t
        return t

    def remove(self, name: str) -> bool:
        return self.triggers.pop(name, None) is not None

    def _fire(self, t: Trigger, event: Optional[dict] = None) -> bool:
        now = self.clock()
        t.fired = [x for x in t.fired if now - x < 3600]
        if (t._task and not t._task.done()) or len(t.fired) >= t.max_per_hour:
            t.skipped += 1
            return False
        task = string.Template(t.task).safe_substitute(
            {k: _plain(v) for k, v in (event or {}).items() if not isinstance(v, (dict, list))}
        )
        t.fired.append(now)
        t._task = asyncio.get_running_loop().create_task(self._go(t, task))
        return True

    async def _go(self, t: Trigger, task: str) -> None:
        try:
            out = await self.runner.run(t.agent, task, owner=t.owner)
            t.last_run = out["id"]
        except Exception as exc:  # noqa: BLE001  one bad run must not stop the loop
            from loguru import logger

            logger.warning(f"trigger {t.name} run failed: {type(exc).__name__}")

    def tick(self) -> list[str]:
        """Start every interval trigger that is due; returns their names."""
        now, started = self.clock(), []
        for t in self.triggers.values():
            if (t.interval_s or t.cron) and now >= t.next_at:
                # no catch-up bursts after downtime
                t.next_at = t.cron.next_after(now) if t.cron else now + t.interval_s
                if self._fire(t):
                    started.append(t.name)
        return started

    def on_bus_event(self, event: dict) -> None:
        """Event-bus sink: ``bus.subscribe(manager.on_bus_event)``."""
        for t in list(self.triggers.values()):
            if t.event == event.get("type") and all(
                event.get(k) == v for k, v in t.match.items()
            ):
                self._fire(t, event)

    async def run(self, poll: float = 1.0) -> None:
        while True:
            self.tick()
            await asyncio.sleep(poll)
