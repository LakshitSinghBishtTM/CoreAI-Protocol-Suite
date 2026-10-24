"""Phase 7: agent harness loop, limits, checkpoints, approvals, kill switch."""

import json

import pytest

from harness import AgentDef, HarnessRunner, RunStore, context, diff_traces
from providers.base import CompletionResponse, Message
from tools import Permission, Tool, ToolRegistry


def resp(text="", calls=None, cost=0.01):
    meta = {"tool_calls": calls} if calls else {}
    return CompletionResponse(text, "m", "p", 10, 5, cost, 1.0, metadata=meta)


def call(name, cid="c1", **args):
    return {"id": cid, "name": name, "arguments": json.dumps(args)}


class Script:
    """Route that replays scripted responses (callables may raise)."""

    def __init__(self, *items):
        self.items, self.requests = list(items), []

    async def __call__(self, req):
        self.requests.append(req)
        item = self.items.pop(0) if len(self.items) > 1 else self.items[0]
        if isinstance(item, Exception):
            raise item
        return item


def make(route, tmp_path=None, **agent):
    reg = ToolRegistry()
    reg.register(
        Tool(
            "echo",
            "echo",
            {"type": "object", "properties": {"x": {"type": "string"}}},
            lambda x="": f"got {x}",
        )
    )
    reg.register(
        Tool(
            "wipe",
            "wipe",
            {"type": "object", "properties": {}},
            lambda: "wiped",
            permission=Permission.DESTRUCTIVE,
        )
    )
    store = RunStore(str(tmp_path / "r.db") if tmp_path else ":memory:")
    d = AgentDef(name="a", system_prompt="sys", allowed_tools=["echo", "wipe"], **agent)
    return HarnessRunner(route, reg, store, {"a": d}), store


async def test_multi_step_task_and_trace():
    r, store = make(Script(resp(calls=[call("echo", x="1")]), resp("done")))
    out = await r.run("a", "go")
    assert out["status"] == "completed" and out["stop_reason"] == "completed"
    assert (
        out["result"] == "done"
        and out["step"] == 2
        and out["cost_usd"] == pytest.approx(0.02)
    )
    types = [e["type"] for e in store.events(out["id"])]
    assert (
        types.count("model_call") == 2
        and "tool_call" in types
        and types[-1] == "run_finished"
    )
    roles = [m.role for m in store.messages(out["id"])]
    assert roles == ["user", "assistant", "tool", "assistant"]


async def test_max_steps_and_cost_limits():
    r, _ = make(
        Script(*[resp(calls=[call("echo", f"c{i}", x=str(i))]) for i in range(9)]),
        max_steps=3,
    )
    assert (await r.run("a", "go"))["stop_reason"] == "max_steps"
    r, _ = make(
        Script(
            *[resp(calls=[call("echo", f"c{i}", x=str(i))], cost=1.0) for i in range(9)]
        ),
        max_cost_usd=1.5,
    )
    out = await r.run("a", "go")
    assert out["stop_reason"] == "cost_limit" and out["step"] == 2


async def test_stall_detection():
    r, _ = make(Script(resp(calls=[call("echo", x="same")])), max_steps=20)
    out = await r.run("a", "go")
    assert out["stop_reason"] == "stalled" and out["step"] == 3


async def test_deadline():
    r, _ = make(Script(resp("x")), deadline_s=-1)
    assert (await r.run("a", "go"))["stop_reason"] == "deadline"


async def test_approval_pause_resume_and_deny():
    for approve, expect in ((True, "wiped"), (False, "Denied")):
        r, store = make(Script(resp(calls=[call("wipe")]), resp("ok")))
        out = await r.run("a", "go")
        assert (
            out["status"] == "waiting_approval"
            and out["pending"]["calls"][0]["name"] == "wipe"
        )
        with pytest.raises(ValueError):
            await r.resume(out["id"])
        out = await r.resume(out["id"], approve=approve)
        assert out["status"] == "completed"
        tool_msg = [m for m in store.messages(out["id"]) if m.role == "tool"][0]
        assert expect in tool_msg.content


async def test_agent_flagged_tool_needs_human():
    r, _ = make(
        Script(resp(calls=[call("echo", x="1")]), resp("ok")), approval_tools=["echo"]
    )
    out = await r.run("a", "go")
    assert out["status"] == "waiting_approval"
    assert (await r.resume(out["id"], approve=True))["status"] == "completed"


async def test_checkpoint_survives_restart(tmp_path):
    boom = RuntimeError("gateway down")
    r, store = make(Script(resp(calls=[call("echo", x="1")]), boom), tmp_path)
    out = await r.run("a", "go")
    assert (
        out["status"] == "failed" and out["stop_reason"] == "error" and out["step"] == 1
    )
    # new process: fresh store and runner on the same file
    r2, store2 = make(Script(resp("recovered")), tmp_path)
    out2 = await r2.resume(out["id"])
    assert (
        out2["status"] == "completed"
        and out2["result"] == "recovered"
        and out2["step"] == 2
    )
    assert [m.role for m in store2.messages(out["id"])][:3] == [
        "user",
        "assistant",
        "tool",
    ]


async def test_pause_cancel_and_kill_switch():
    r, store = make(Script(resp("x")))
    rid = r.create("a", "go", owner="alice")
    r.pause(rid)
    out = await r.execute(rid)
    assert out["status"] == "paused"
    assert (await r.resume(rid))["status"] == "completed"

    a = r.create("a", "go", owner="alice")
    b = r.create("a", "go", owner="bob")
    assert r.kill("alice") == 1
    assert (await r.execute(a))["stop_reason"] == "killed"
    assert (await r.execute(b))["status"] == "completed"
    c = r.create("a", "go", owner="bob")
    assert r.cancel(c) and (await r.execute(c))["status"] == "cancelled"
    assert not r.cancel(a)  # already finished


async def test_compaction_keeps_task_and_tool_pairs():
    task = Message("user", "THE TASK")
    msgs = [task]
    for i in range(10):
        msgs.append(Message("assistant", "", tool_calls=[call("echo", f"c{i}", x="y")]))
        msgs.append(Message("tool", "z" * 2000, tool_call_id=f"c{i}"))
    out, changed = await context.compact(
        msgs, system="sys", max_tokens=1500, keep_recent=4
    )
    assert changed and out[0] is task and context.count(out, "sys") <= 1500
    assert out[1].content.startswith(context.SUMMARY_PREFIX)
    ids = {c["id"] for m in out for c in (m.tool_calls or [])}
    assert all(m.tool_call_id in ids for m in out if m.role == "tool")
    same, changed = await context.compact(msgs[:3], system="", max_tokens=10**6)
    assert not changed and same == msgs[:3]


async def test_local_model_gets_tighter_limits():
    d = AgentDef(
        name="a", system_prompt="s", local=True, max_steps=50, context_tokens=32000
    )
    e = d.effective()
    assert e.max_steps == 8 and e.context_tokens == 6000 and d.max_steps == 50
    assert (
        AgentDef(name="a", system_prompt="s", max_steps=3, local=True)
        .effective()
        .max_steps
        == 3
    )
    with pytest.raises(ValueError):
        AgentDef.from_dict({"name": "a", "system_prompt": "s", "bogus": 1})


async def test_trace_diff_and_stream():
    r, store = make(Script(resp(calls=[call("echo", x="1")]), resp("done")))
    a = await r.run("a", "go")
    r2, store2 = make(Script(resp("done")))
    b = await r2.run("a", "go")
    assert diff_traces(store.events(a["id"]), store.events(a["id"])) == []
    assert diff_traces(store.events(a["id"]), store2.events(b["id"]))
    seen = [e["type"] async for e in r.stream(a["id"])]
    assert seen[0] == "run_started" and seen[-1] == "run_finished"


@pytest.mark.asyncio
async def test_agent_memory_recalled_into_prompt_owner_scoped():
    from memory import MemoryStore

    mem = MemoryStore(":memory:")
    mem.remember("alice", "color", "alice likes teal", ns="prefs")
    mem.remember("bob", "color", "bob likes red", ns="prefs")
    mem.remember("alice", "color", "other ns teal", ns="other")
    route = Script(resp("done"))
    runner, _ = make(route, memory_ns="prefs")
    runner.memory = mem
    await runner.run("a", "what color do I like", owner="alice")
    sysmsg = route.requests[0].system_prompt
    assert "alice likes teal" in sysmsg
    assert "bob likes red" not in sysmsg and "other ns" not in sysmsg
    assert sysmsg.startswith("sys")


@pytest.mark.asyncio
async def test_agent_memory_off_by_default():
    from memory import MemoryStore

    mem = MemoryStore(":memory:")
    mem.remember("alice", "color", "teal", ns="prefs")
    route = Script(resp("done"))
    runner, _ = make(route)  # no memory_ns
    runner.memory = mem
    await runner.run("a", "color", owner="alice")
    assert route.requests[0].system_prompt == "sys"


@pytest.mark.parametrize("approve,expect", [(True, "approved"), (False, "declined")])
async def test_agent_flagged_uncertainty_pauses_for_human(approve, expect):
    route = Script(
        resp(calls=[call("request_human_review", question="delete?")]), resp("ok")
    )
    r, store = make(route, ask_when_uncertain=True)
    out = await r.run("a", "go")
    assert out["status"] == "waiting_approval"
    assert any(
        t["function"]["name"] == "request_human_review"
        for t in route.requests[0].tools
    )
    out = await r.resume(out["id"], approve=approve)
    assert out["status"] == "completed"
    tool_msg = [m for m in store.messages(out["id"]) if m.role == "tool"][0]
    assert expect in tool_msg.content


async def test_review_tool_not_offered_by_default():
    route = Script(resp("done"))
    r, _ = make(route)
    await r.run("a", "go")
    names = [t["function"]["name"] for t in route.requests[0].tools]
    assert "request_human_review" not in names


async def test_replay_reasks_model_without_running_tools():
    from harness import replay_run

    r, _ = make(Script(resp(calls=[call("wipe")]), resp("done")), approval_tools=[])
    out = await r.run("a", "go")
    assert out["status"] == "waiting_approval"  # destructive: never executed
    # same behaviour replayed
    same = await replay_run(r, out["id"], route=Script(resp(calls=[call("wipe")])))
    assert same["turns"][0]["same_arguments"] and same["matching_turns"] == 1
    # a different model that answers in text instead
    diff = await replay_run(
        r, out["id"], model="other", route=Script(resp("no tools needed"))
    )
    assert diff["turns"][0]["same_tools"] is False
    assert diff["turns"][0]["replayed_calls"] == []
    assert not any(m.role == "tool" for m in r.store.messages(out["id"]))


async def test_subagent_runs_as_tool_with_own_trace_and_limits():
    from harness import subagent_tool

    child = AgentDef(name="child", system_prompt="c", max_steps=3)
    parent = AgentDef(name="a", system_prompt="p", allowed_tools=["agent_child"])
    route = Script(
        resp(calls=[call("agent_child", task="sub task")]),  # parent
        resp("child answer"),  # child
        resp("parent done"),  # parent after tool result
    )
    r, store = make(route)
    r.agents.update({"a": parent, "child": child})
    r.registry.register(subagent_tool(r, "child"))
    out = await r.run("a", "go")
    assert out["status"] == "completed" and out["result"] == "parent done"
    tool_msg = [m for m in store.messages(out["id"]) if m.role == "tool"][0]
    assert "child answer" in tool_msg.content  # wrapped as untrusted tool output


async def test_subagent_nesting_is_capped():
    from harness import subagent_tool
    from harness.subagent import MAX_DEPTH, _depth

    child = AgentDef(name="child", system_prompt="c")
    r, _ = make(Script(resp("x")))
    r.agents["child"] = child
    tool = subagent_tool(r, "child")
    token = _depth.set(MAX_DEPTH)
    try:
        assert "nesting limit" in await tool.handler("t")
    finally:
        _depth.reset(token)
    with pytest.raises(KeyError):
        subagent_tool(r, "missing")
