import asyncio

from coreai.cache import ResponseCache
from coreai.orchestrator import Orchestrator
from providers.base import CompletionRequest, Message, split_system
from providers.anthropic import AnthropicProvider


def test_cache_key_changes_with_every_option():
    c = ResponseCache()
    m = [{"role": "user", "content": "hi"}]
    base = {"system_prompt": None, "temperature": 0.7, "max_tokens": 50}
    k = c._make_key("p", "m", m, base)
    for opt, val in [("system_prompt", "be brief"), ("temperature", 0.1), ("max_tokens", 2000)]:
        assert c._make_key("p", "m", m, {**base, opt: val}) != k


def test_cache_miss_then_hit_respects_options():
    async def run():
        c = ResponseCache()
        m = [{"role": "user", "content": "hi"}]
        await c.set("p", "m", m, {"content": "x"}, options={"max_tokens": 50})
        assert await c.get("p", "m", m, {"max_tokens": 2000}) is None
        assert await c.get("p", "m", m, {"max_tokens": 50}) == {"content": "x"}

    asyncio.run(run())


def test_split_system_merges_prompt_and_system_messages():
    req = CompletionRequest(
        messages=[Message("system", "Answer in French"), Message("user", "hi")],
        system_prompt="Be brief",
    )
    system, rest = split_system(req)
    assert system == "Be brief\n\nAnswer in French"
    assert [m.role for m in rest] == ["user"]


def test_anthropic_keeps_system_messages():
    p = AnthropicProvider.__new__(AnthropicProvider)
    req = CompletionRequest(messages=[Message("system", "Answer in French"), Message("user", "hi")])
    assert split_system(req)[0] == "Answer in French"
    assert p._build_messages(req) == [{"role": "user", "content": "hi"}]


def test_task_priority_is_stored_and_ordered():
    o = Orchestrator()
    o.register_agent("a")
    lo = o.assign_task("a", "low", priority=0)
    hi = o.assign_task("a", "high", priority=3)
    assert hi.priority == 3
    from coreai.orchestrator import TaskStatus
    assert [t.task_id for t in o.task_store.list_by_status(TaskStatus.PENDING)] == [hi.task_id, lo.task_id]


# ---- second review ---------------------------------------------------------
import hashlib
import time
from types import SimpleNamespace

import pytest

from coreai.task_runner import TaskRunner


class _Agent:
    def __init__(self, delay=0.0, result="done"):
        self.delay, self.result, self.ran = delay, result, []

    async def execute(self, task):
        self.ran.append(task["instruction"])
        await asyncio.sleep(self.delay)
        return self.result


def _setup(agent, timeout=5.0):
    orch = Orchestrator()
    mgr = SimpleNamespace(agents={"a1": SimpleNamespace(agent=agent)})
    orch.register_agent("a1")
    return orch, TaskRunner(orch, mgr, timeout_s=timeout)


async def _wait(orch, task_id, status, n=200):
    for _ in range(n):
        if orch.get_task(task_id).status.value == status:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(orch.get_task(task_id).status)


def test_task_runs_to_a_result():
    async def run():
        orch, runner = _setup(_Agent())
        t = orch.assign_task("a1", "do it")
        runner.submit(t.task_id)
        await _wait(orch, t.task_id, "completed")
        assert orch.get_task(t.task_id).result == "done"
        await runner.shutdown()

    asyncio.run(run())


def test_higher_priority_runs_first_and_cancel_works():
    async def run():
        agent = _Agent(delay=0.05)
        orch, runner = _setup(agent)
        first = orch.assign_task("a1", "first", priority=2)
        runner.submit(first.task_id)
        await asyncio.sleep(0.01)  # first is running
        low = orch.assign_task("a1", "low", priority=0)
        high = orch.assign_task("a1", "high", priority=3)
        gone = orch.assign_task("a1", "cancelled", priority=3)
        for t in (low, high, gone):
            runner.submit(t.task_id)
        assert runner.cancel(gone.task_id)
        await _wait(orch, low.task_id, "completed")
        assert agent.ran == ["first", "high", "low"]
        assert orch.get_task(gone.task_id).status.value == "cancelled"
        await runner.shutdown()

    asyncio.run(run())


def test_failure_and_timeout_are_recorded():
    async def run():
        orch, runner = _setup(_Agent(delay=1), timeout=0.05)
        t = orch.assign_task("a1", "slow")
        runner.submit(t.task_id)
        await _wait(orch, t.task_id, "failed")
        assert "timed out" in orch.get_task(t.task_id).error
        await runner.shutdown()

    asyncio.run(run())


def test_middleware_separates_bearer_clients_on_one_ip():
    from api.middleware import RateLimitMiddleware

    mw = RateLimitMiddleware.__new__(RateLimitMiddleware)

    def req(auth):
        return SimpleNamespace(
            headers={"Authorization": auth}, client=SimpleNamespace(host="1.1.1.1")
        )

    a, b = mw._get_client_key(req("Bearer eyJhbGci.aaa")), mw._get_client_key(req("Bearer eyJhbGci.bbb"))
    assert a != b and a.startswith("bearer:") and "aaa" not in a


def test_cache_hit_reports_current_latency():
    from tests.helpers import make_provider, make_router

    async def run():
        router = make_router({"a": make_provider("a")}, enable_cache=True)
        req = lambda: CompletionRequest(messages=[Message("user", "hello")])
        first = await router.route(req())
        first_cached = await router.route(req())
        assert first_cached.cached and first_cached.cost_usd == 0.0
        assert first_cached.latency_ms < 1000
        assert "original_latency_ms" in first_cached.metadata

    asyncio.run(run())
