"""Phase 6: gateway tool-execution loop."""

import json

from providers.base import CompletionRequest, CompletionResponse, Message
from tools import Permission, Tool, ToolRegistry
from tools.builtin import register_builtins
from tools.executor import run_tool_loop


def call(name, args, cid="c1"):
    return {"id": cid, "name": name, "arguments": json.dumps(args)}


def resp(text="", calls=None, cost=0.01):
    meta = {"tool_calls": calls} if calls else {}
    return CompletionResponse(
        content=text, model="m", provider="p", input_tokens=1, output_tokens=1,
        cost_usd=cost, latency_ms=1.0, metadata=meta,
    )  # fmt: skip


def scripted(*responses):
    seen = []
    it = iter(responses)

    async def route(req):
        seen.append(req)
        return next(it)

    return route, seen


def request():
    return CompletionRequest(messages=[Message(role="user", content="2+2?")])


def calc_registry():
    r = ToolRegistry()
    register_builtins(r)
    return r


async def test_runs_tool_and_continues_to_final_answer():
    route, seen = scripted(resp(calls=[call("calculator", {"expression": "2+2"})]), resp("four"))
    out = await run_tool_loop(route, calc_registry(), request(), allowed=["calculator"])
    assert out.response.content == "four" and out.stop_reason == "answered"
    assert out.steps[0].ok and "4" in out.steps[0].output
    assert "untrusted" in out.steps[0].output
    # second model call saw the assistant tool call and the tool result
    roles = [m.role for m in seen[1].messages]
    assert roles == ["user", "assistant", "tool"]
    assert seen[1].messages[2].tool_call_id == "c1"
    assert abs(out.total_cost_usd - 0.02) < 1e-9


async def test_only_allowed_tools_are_offered_and_called():
    route, seen = scripted(resp(calls=[call("calculator", {"expression": "1+1"})]), resp("done"))
    out = await run_tool_loop(route, calc_registry(), request(), allowed=[])
    assert seen[0].tools is None  # deny by default: nothing offered
    assert not out.steps[0].ok and out.steps[0].error_code == "not_allowed"
    assert out.response.content == "done"  # model told, loop continued


async def test_bad_arguments_are_fed_back_not_raised():
    route, _ = scripted(
        resp(calls=[{"id": "c1", "name": "calculator", "arguments": "{nope"}]), resp("ok")
    )
    out = await run_tool_loop(route, calc_registry(), request(), allowed=["*"])
    assert out.steps[0].error_code == "invalid_arguments" and out.response.content == "ok"


async def test_max_steps_stops_a_runaway_model():
    calls = [resp(calls=[call("calculator", {"expression": "1"}, f"c{i}")]) for i in range(10)]
    route, seen = scripted(*calls)
    out = await run_tool_loop(route, calc_registry(), request(), allowed=["*"], max_steps=2)
    assert out.stop_reason == "max_steps" and len(out.steps) == 2
    assert len(seen) == 3


async def test_cost_limit_stops_loop():
    calls = [resp(calls=[call("calculator", {"expression": "1"}, f"c{i}")], cost=1.0) for i in range(5)]
    route, _ = scripted(*calls)
    out = await run_tool_loop(route, calc_registry(), request(), allowed=["*"], max_cost_usd=1.5)
    assert out.stop_reason == "cost_limit" and len(out.steps) == 1


async def test_destructive_tool_pauses_for_approval_then_runs_when_approved():
    r = ToolRegistry()
    ran = []
    r.register(Tool("wipe", "w", {"type": "object"}, lambda: ran.append(1) or "wiped",
                    permission=Permission.DESTRUCTIVE))  # fmt: skip
    route, _ = scripted(resp(calls=[call("wipe", {})]))
    out = await run_tool_loop(route, r, request(), allowed=["wipe"])
    assert out.stop_reason == "needs_approval" and out.pending_approval.tool == "wipe"
    assert not ran
    route2, _ = scripted(resp(calls=[call("wipe", {})]), resp("finished"))
    out2 = await run_tool_loop(route2, r, request(), allowed=["wipe"], approved=["wipe"])
    assert ran == [1] and out2.response.content == "finished"
