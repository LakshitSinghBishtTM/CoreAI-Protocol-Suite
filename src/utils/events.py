"""Event bus for outbound notifications: run completion, budget alerts, provider outages.

Sinks are callables taking one event dict (sync or async). Emitting never blocks
or raises: delivery is fire-and-forget on the running loop, failures are logged.

Env configuration (``configure_from_env``):
  CAIPS_EVENT_WEBHOOK_URL, CAIPS_EVENT_WEBHOOK_SECRET   signed webhook (both required)
  CAIPS_EVENT_WEBHOOK_EVENTS                            optional comma list, e.g. run.finished,budget.exceeded
  CAIPS_SLACK_WEBHOOK_URL [, CAIPS_SLACK_EVENTS]        Slack incoming webhook
  CAIPS_SMTP_HOST, CAIPS_SMTP_FROM, CAIPS_ALERT_EMAIL_TO  email (also CAIPS_SMTP_PORT=587,
      CAIPS_SMTP_USER, CAIPS_SMTP_PASSWORD, CAIPS_SMTP_STARTTLS=1, CAIPS_EMAIL_EVENTS)
Slack and email default to alert events only (budget.*, provider.*, failed runs).

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
import smtplib
import ssl
import time
import uuid
from email.message import EmailMessage
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


ALERT_EVENTS = ("budget.warning", "budget.exceeded", "provider.outage", "provider.recovered")


def is_alert(event: dict) -> bool:
    """Default for chat and email: alerts, plus runs that failed (not clean finishes)."""
    return event["type"] in ALERT_EVENTS or (event["type"] == "run.finished" and event.get("status") == "failed")


def format_event(event: dict) -> tuple[str, str]:
    """(subject, body) in plain text. Contains no secrets: only ids, names and amounts."""
    t = event["type"]
    if t == "run.finished":
        subject = f"Run {event.get('run_id')} {event.get('status')} ({event.get('reason')})"
    elif t.startswith("budget."):
        subject = (
            f"Budget {t.split('.')[1]}: {event.get('subject')} at ${event.get('used_usd')} of ${event.get('cap_usd')}"
        )
    elif t.startswith("provider."):
        subject = f"Provider {event.get('provider')}: {t.split('.')[1]}"
    else:
        subject = t
    body = "\n".join(f"{k}: {v}" for k, v in event.items() if k not in ("id",))
    return subject, body


class SlackSink:
    """Slack incoming webhook. ``events`` limits types; default is alerts only."""

    def __init__(self, url: str, events: Optional[Iterable[str]] = None, transport=None):
        self.url, self.transport = url, transport
        self.events = set(events) if events else None

    async def __call__(self, event: dict) -> None:
        if (self.events is not None and event["type"] not in self.events) or (
            self.events is None and not is_alert(event)
        ):
            return
        subject, body = format_event(event)
        async with httpx.AsyncClient(timeout=5, transport=self.transport) as http:
            (await http.post(self.url, json={"text": f"*{subject}*\n```{body}```"})).raise_for_status()


class EmailSink:
    def __init__(
        self, host: str, sender: str, to: list[str], port: int = 587, user: str = "", password: str = "",
        starttls: bool = True, events: Optional[Iterable[str]] = None, smtp_factory=smtplib.SMTP,
    ):  # fmt: skip
        self.host, self.sender, self.to, self.port = host, sender, to, port
        self.user, self.password, self.starttls = user, password, starttls
        self.events = set(events) if events else None
        self.smtp_factory = smtp_factory

    def _send(self, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self.sender, ", ".join(self.to), f"[CaiPS] {subject}"
        msg.set_content(body)
        with self.smtp_factory(self.host, self.port, timeout=10) as smtp:
            if self.starttls:
                smtp.starttls(context=ssl.create_default_context())
            if self.user:
                smtp.login(self.user, self.password)
            smtp.send_message(msg)

    async def __call__(self, event: dict) -> None:
        if (self.events is not None and event["type"] not in self.events) or (
            self.events is None and not is_alert(event)
        ):
            return
        await asyncio.to_thread(self._send, *format_event(event))


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
        def csv(name: str) -> list[str]:
            items = [e.strip() for e in os.getenv(name, "").split(",") if e.strip()]
            unknown = [e for e in items if e not in EVENT_TYPES]
            if unknown:
                logger.warning(f"unknown event types in {name}: {unknown}")
            return items

        url = os.getenv("CAIPS_EVENT_WEBHOOK_URL", "").strip()
        if url:
            secret = os.getenv("CAIPS_EVENT_WEBHOOK_SECRET", "")
            if secret:
                self.subscribe(WebhookSink(url, secret, csv("CAIPS_EVENT_WEBHOOK_EVENTS") or None))
            else:
                logger.warning("CAIPS_EVENT_WEBHOOK_URL set without CAIPS_EVENT_WEBHOOK_SECRET; webhook disabled")
        slack = os.getenv("CAIPS_SLACK_WEBHOOK_URL", "").strip()
        if slack:
            self.subscribe(SlackSink(slack, csv("CAIPS_SLACK_EVENTS") or None))
        host, sender = os.getenv("CAIPS_SMTP_HOST", "").strip(), os.getenv("CAIPS_SMTP_FROM", "").strip()
        to = [a.strip() for a in os.getenv("CAIPS_ALERT_EMAIL_TO", "").split(",") if a.strip()]
        if host and sender and to:
            self.subscribe(
                EmailSink(
                    host, sender, to, int(os.getenv("CAIPS_SMTP_PORT", "587")),
                    os.getenv("CAIPS_SMTP_USER", ""), os.getenv("CAIPS_SMTP_PASSWORD", ""),
                    os.getenv("CAIPS_SMTP_STARTTLS", "1") != "0", csv("CAIPS_EMAIL_EVENTS") or None,
                )
            )  # fmt: skip
        elif host or sender or to:
            logger.warning("email alerts need CAIPS_SMTP_HOST, CAIPS_SMTP_FROM and CAIPS_ALERT_EMAIL_TO; disabled")


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
