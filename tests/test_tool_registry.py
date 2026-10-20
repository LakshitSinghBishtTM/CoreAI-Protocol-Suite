"""Phase 6: tool registry, permissions, validation, limits."""

import asyncio

import pytest

from tools import ApprovalRequired, Permission, Tool, ToolError, ToolRegistry

ADD = {
    "type": "object",
    "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
    "required": ["a", "b"],
    "additionalProperties": False,
}


def reg(**kw):
    r = ToolRegistry()
    r.register(Tool("add", "Add two ints", ADD, lambda a, b: a + b, **kw))
    return r


async def test_call_returns_untrusted_labelled_output():
    res = await reg().call("add", {"a": 1, "b": 2}, allowed=["add"])
    assert res.content == "3" and res.untrusted
    assert 'trust="untrusted"' in res.as_untrusted()


async def test_deny_by_default_and_wildcard():
    r = reg()
    assert r.visible() == [] and r.specs(None) == []
    with pytest.raises(ToolError) as e:
        await r.call("add", {"a": 1, "b": 2})
    assert e.value.code == "not_allowed"
    assert (await r.call("add", {"a": 1, "b": 2}, allowed=["*"])).content == "3"


async def test_schema_validation_rejects_bad_arguments():
    r = reg()
    for bad in ({"a": 1}, {"a": "x", "b": 2}, {"a": 1, "b": 2, "c": 3}):
        with pytest.raises(ToolError) as e:
            await r.call("add", bad, allowed=["add"])
        assert e.value.code == "invalid_arguments"


async def test_destructive_needs_approval_and_permission_cap():
    r = reg(permission=Permission.DESTRUCTIVE)
    with pytest.raises(ApprovalRequired):
        await r.call("add", {"a": 1, "b": 2}, allowed=["add"])
    ok = await r.call("add", {"a": 1, "b": 2}, allowed=["add"], approved=True)
    assert ok.content == "3"
    with pytest.raises(ToolError) as e:
        await r.call("add", {"a": 1, "b": 2}, allowed=["add"], approved=True,
                     max_permission=Permission.WRITE)  # fmt: skip
    assert e.value.code == "permission_denied"


async def test_timeout_and_failure_are_tool_errors():
    r = ToolRegistry()

    async def slow():
        await asyncio.sleep(5)

    def boom():
        raise RuntimeError("no")

    empty = {"type": "object", "properties": {}}
    r.register(Tool("slow", "s", empty, slow, timeout_s=0.05))
    r.register(Tool("boom", "b", empty, boom))
    with pytest.raises(ToolError) as e:
        await r.call("slow", {}, allowed=["*"])
    assert e.value.code == "timeout"
    with pytest.raises(ToolError) as e:
        await r.call("boom", {}, allowed=["*"])
    assert e.value.code == "tool_failed"


async def test_output_is_capped():
    r = ToolRegistry()
    r.register(Tool("big", "b", {"type": "object"}, lambda: "x" * 50_000))
    res = await r.call("big", {}, allowed=["*"])
    assert res.truncated and len(res.content) < 21_000


async def test_audit_hook_sees_success_and_failure_and_cannot_break_calls():
    seen = []
    r = reg()
    r.on_call = lambda *a: seen.append((a[0], a[1], type(a[3]).__name__))
    await r.call("add", {"a": 1, "b": 2}, allowed=["add"], caller="k1")
    with pytest.raises(ToolError):
        await r.call("add", {"a": 1}, allowed=["add"], caller="k1")
    assert seen == [("k1", "add", "ToolResult"), ("k1", "add", "ToolError")]
    r.on_call = lambda *a: 1 / 0
    assert (await r.call("add", {"a": 1, "b": 1}, allowed=["add"])).content == "2"


def test_registration_rules():
    r = reg()
    with pytest.raises(ValueError):
        r.register(Tool("add", "dup", ADD, lambda a, b: 0))
    with pytest.raises(ValueError):
        r.register(Tool("bad name!", "x", ADD, lambda a, b: 0))
    with pytest.raises(Exception):
        r.register(Tool("badschema", "x", {"type": "nope"}, lambda: 0))
    assert r.specs(["add"])[0]["function"]["name"] == "add"
