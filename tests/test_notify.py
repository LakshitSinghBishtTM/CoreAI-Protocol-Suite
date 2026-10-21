import json

import httpx
import pytest

from coreai.budgets import BudgetExceeded, BudgetLimits, BudgetManager
from coreai.notify import WebhookNotifier, sign


def capture():
    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200)

    return seen, httpx.MockTransport(handler)


async def test_notifier_signs_body():
    seen, transport = capture()
    n = WebhookNotifier("https://hooks.example/x", "s3cret", transport=transport)
    assert await n.send({"event": "budget.warning", "subject": "u"}) is True
    req = seen[0]
    ts = req.headers["X-CaiPS-Timestamp"]
    assert req.headers["X-CaiPS-Signature"] == sign("s3cret", ts, req.content.decode())
    assert json.loads(req.content)["event"] == "budget.warning"


async def test_failed_delivery_is_swallowed_and_counted():
    n = WebhookNotifier(
        "https://hooks.example/x", "s",
        transport=httpx.MockTransport(lambda r: httpx.Response(500)),
    )  # fmt: skip
    assert await n.send({"event": "x"}) is False and n.failed == 1


def test_notifier_requires_secret_and_http_url():
    with pytest.raises(ValueError):
        WebhookNotifier("https://x", "")
    with pytest.raises(ValueError):
        WebhookNotifier("file:///etc/passwd", "s")


def mgr(events, **limits):
    m = BudgetManager(
        default=BudgetLimits(window_seconds=3600, **limits), clock=lambda: 1000.0
    )
    m.on_alert = events.append
    return m


def test_warning_fires_once_per_window():
    ev = []
    m = mgr(ev, max_cost_usd=10, warn_cost_usd=1)
    m.record("u", cost_usd=0.5)
    assert ev == []
    m.record("u", cost_usd=0.6)
    m.record("u", cost_usd=0.6)
    assert [e["event"] for e in ev] == ["budget.warning"]
    assert ev[0]["subject"] == "u" and ev[0]["used"]["cost_usd"] == 1.1


def test_exceeded_alerts_once_and_still_raises():
    ev = []
    m = mgr(ev, max_requests=1)
    m.record("u")
    for _ in range(3):
        with pytest.raises(BudgetExceeded):
            m.check("u")
    assert [e["event"] for e in ev] == ["budget.exceeded"]
    assert ev[0]["limit"] == "requests" and ev[0]["cap"] == 1


def test_alert_hook_errors_do_not_break_accounting():
    m = mgr([], max_cost_usd=10, warn_cost_usd=1)
    m.on_alert = lambda e: 1 / 0
    m.record("u", cost_usd=2)  # must not raise
    assert m.status("u")["used"]["cost_usd"] == 2


def test_model_budget_alert_names_the_model_not_a_subject():
    ev = []
    m = BudgetManager(
        models={"gpt-x": BudgetLimits(warn_cost_usd=1, window_seconds=60)},
        clock=lambda: 5.0,
    )
    m.on_alert = ev.append
    m.record_model("gpt-x", cost_usd=2)
    assert ev[0]["model"] == "gpt-x" and "subject" not in ev[0]
