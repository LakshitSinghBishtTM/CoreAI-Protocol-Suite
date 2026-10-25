"""Planning step: one tool-free call before the first step."""

import pytest

from harness.runner import PLAN_MARK
from tests.test_harness import Script, call, make, resp


async def test_plan_runs_first_without_tools_and_is_kept_in_the_task():
    route = Script(
        resp("1. echo it\n2. answer"), resp(calls=[call("echo", x="1")]), resp("done")
    )
    r, store = make(route, plan=True)
    out = await r.run("a", "go")
    assert out["status"] == "completed" and out["step"] == 2  # plan is not a step
    plan_req, first_step = route.requests[0], route.requests[1]
    assert plan_req.tools is None and plan_req.metadata["purpose"] == "plan"
    assert "at most 6 steps" in plan_req.messages[0].content
    assert first_step.tools  # tools are offered on real steps
    task = store.messages(out["id"])[0].content
    assert task.startswith("go") and PLAN_MARK in task and "1. echo it" in task
    assert "1. echo it" in first_step.messages[0].content
    assert out["cost_usd"] == pytest.approx(0.03)  # plan cost is counted
    assert "plan" in [e["type"] for e in store.events(out["id"])]


async def test_plan_is_off_by_default():
    route = Script(resp("done"))
    r, store = make(route)
    await r.run("a", "go")
    assert len(route.requests) == 1
    assert PLAN_MARK not in store.messages(r.store.list_runs()[0]["id"])[0].content


async def test_plan_is_not_repeated_on_resume():
    route = Script(resp("the plan"), resp(calls=[call("wipe")]), resp("done"))
    r, store = make(route, plan=True, approval_tools=["wipe"])
    out = await r.run("a", "go")
    assert out["status"] == "waiting_approval"
    out = await r.resume(out["id"], approve=True)
    assert out["status"] == "completed"
    plans = [e for e in store.events(out["id"]) if e["type"] == "plan"]
    assert len(plans) == 1
    assert store.messages(out["id"])[0].content.count(PLAN_MARK) == 1


async def test_plan_counts_toward_the_cost_limit():
    route = Script(resp("plan", cost=0.5), resp("done"))
    r, _ = make(route, plan=True, max_cost_usd=0.4)
    out = await r.run("a", "go")
    assert out["stop_reason"] == "cost_limit" and len(route.requests) == 1


async def test_empty_plan_leaves_the_task_unchanged():
    route = Script(resp("  "), resp("done"))
    r, store = make(route, plan=True)
    out = await r.run("a", "go")
    assert out["status"] == "completed"
    assert store.messages(out["id"])[0].content == "go"
