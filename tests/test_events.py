import asyncio
import json

import httpx

from coreai.budgets import BudgetLimits, BudgetManager
from coreai.circuit_breaker import CircuitBreaker
from utils.events import EventBus, WebhookSink, sign, watch_budgets


def test_webhook_signed_and_filtered():
    seen = []

    def h(req):
        seen.append(req)
        return httpx.Response(200)

    async def go():
        bus = EventBus()
        bus.subscribe(WebhookSink("http://x/h", "s3", ["run.finished"], transport=httpx.MockTransport(h)))
        bus.emit("budget.warning", subject="k")
        bus.emit("run.finished", run_id="r1")
        await asyncio.gather(*bus.tasks)

    asyncio.run(go())
    assert len(seen) == 1
    r = seen[0]
    ts, body = r.headers["x-caips-timestamp"], r.content.decode()
    assert r.headers["x-caips-signature"] == sign("s3", ts, body)
    assert json.loads(body)["run_id"] == "r1"


def test_failing_and_loopless_sinks_never_raise():
    bus = EventBus()
    bus.subscribe(lambda e: 1 / 0)

    async def bad(e):
        raise RuntimeError("down")

    bus.subscribe(bad)
    bus.emit("run.finished")  # no running loop: dropped quietly

    async def go():
        bus.emit("run.finished")
        await asyncio.gather(*bus.tasks)

    asyncio.run(go())


def test_env_config(monkeypatch):
    bus = EventBus()
    monkeypatch.setenv("CAIPS_EVENT_WEBHOOK_URL", "http://x")
    bus.configure_from_env()
    assert bus.sinks == []  # no secret, no webhook
    monkeypatch.setenv("CAIPS_EVENT_WEBHOOK_SECRET", "s")
    monkeypatch.setenv("CAIPS_EVENT_WEBHOOK_EVENTS", "run.finished")
    bus.configure_from_env()
    assert len(bus.sinks) == 1 and bus.sinks[0].events == {"run.finished"}


def test_budget_alerts_fire_once_per_window():
    got = []
    m = BudgetManager(default=BudgetLimits(max_cost_usd=10, warn_cost_usd=5))
    watch_budgets(m, lambda t, **kw: got.append((t.split(".")[1], kw["subject"], kw["used_usd"], kw["cap_usd"])))
    m.record("k", cost_usd=4)
    assert got == []
    m.record("k", cost_usd=1.5)
    m.add_usage("k", cost_usd=0.5)
    assert [g[0] for g in got] == ["warning"]
    m.record("k", cost_usd=5)
    m.record("k", cost_usd=5)
    assert [g[0] for g in got] == ["warning", "exceeded"]


def test_breaker_reports_open_and_close_with_fake_clock():
    t = [0.0]
    got = []
    b = CircuitBreaker(
        "p", failure_threshold=2, reset_timeout=10, clock=lambda: t[0], on_change=lambda *a: got.append(a)
    )
    b.record_failure()
    b.record_failure()
    assert got == [("p", "open")]
    t[0] = 11
    assert b.allow()
    b.record_success()
    assert got == [("p", "open"), ("p", "closed")]


def test_run_finished_event(tmp_path):
    import asyncio as aio

    from harness.config import AgentDef
    from harness.runner import HarnessRunner
    from harness.store import RunStore
    from providers.base import CompletionResponse
    from tools.registry import ToolRegistry
    from utils.events import bus

    async def route(req):
        return CompletionResponse(content="done", model="m", provider="p", input_tokens=1, output_tokens=1,
                                  cost_usd=0.01, latency_ms=1.0)  # fmt: skip

    events = []
    bus.clear()
    bus.subscribe(events.append)
    try:
        runner = HarnessRunner(
            route, ToolRegistry(), RunStore(), {"a": AgentDef.from_dict({"name": "a", "system_prompt": "x"})}
        )
        aio.run(runner.run("a", "hi", owner="alice"))
    finally:
        bus.clear()
    fin = [e for e in events if e["type"] == "run.finished"]
    assert len(fin) == 1 and fin[0]["status"] == "completed" and fin[0]["owner"] == "alice"


def test_budget_alerts_rearm_after_window_reset():
    t = [0.0]
    got = []
    m = BudgetManager(default=BudgetLimits(max_cost_usd=10, warn_cost_usd=5, window_seconds=100), clock=lambda: t[0])
    watch_budgets(m, lambda ty, **kw: got.append(ty))
    m.record("k", cost_usd=6)
    t[0] = 150  # new window
    m.record("k", cost_usd=1)
    m.record("k", cost_usd=6)
    assert got == ["budget.warning", "budget.warning"]
