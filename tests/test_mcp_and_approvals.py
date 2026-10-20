"""Phase 6: MCP server, approvals, server-side tool config."""

import pytest

from tools import Permission, Tool
from tools.approvals import ApprovalStore
from tools.config import build_runtime


def rt(**extra):
    cfg = {"grants": {"default": ["calculator"], "admin": ["*"]}, **extra}
    r = build_runtime(cfg)
    r.registry.register(
        Tool("wipe", "w", {"type": "object"}, lambda: "wiped", permission=Permission.DESTRUCTIVE)
    )
    return r


def rpc(method, params=None, mid=1):
    m = {"jsonrpc": "2.0", "method": method, "id": mid}
    if params is not None:
        m["params"] = params
    return m


async def test_initialize_ping_and_notification():
    s = rt().mcp
    out = await s.handle(rpc("initialize", {}))
    assert out["result"]["serverInfo"]["name"] == "caips-tools"
    assert (await s.handle(rpc("ping")))["result"] == {}
    assert await s.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert (await s.handle({"nope": 1}))["error"]["code"] == -32600
    assert (await s.handle(rpc("bogus")))["error"]["code"] == -32601


async def test_tools_list_respects_server_side_grants():
    s = rt().mcp
    names = lambda r: [t["name"] for t in r["result"]["tools"]]  # noqa: E731
    assert names(await s.handle(rpc("tools/list"), "bob")) == ["calculator"]
    assert "wipe" in names(await s.handle(rpc("tools/list"), "admin"))
    assert (await s.handle(rpc("tools/list"), "bob"))["result"]["tools"][0]["inputSchema"]


async def test_tools_call_success_and_errors_are_in_band():
    s = rt().mcp
    ok = await s.handle(rpc("tools/call", {"name": "calculator", "arguments": {"expression": "6*7"}}), "bob")
    assert ok["result"]["isError"] is False and "42" in ok["result"]["content"][0]["text"]
    bad = await s.handle(rpc("tools/call", {"name": "wipe", "arguments": {}}), "bob")
    assert bad["result"]["isError"] and "not_allowed" in bad["result"]["content"][0]["text"]
    inv = await s.handle(rpc("tools/call", {"name": "calculator", "arguments": {}}), "bob")
    assert "invalid_arguments" in inv["result"]["content"][0]["text"]
    assert (await s.handle(rpc("tools/call", {}), "bob"))["error"]["code"] == -32602


async def test_destructive_over_mcp_needs_approval_then_runs_once():
    r = rt(max_permission="destructive")
    call = rpc("tools/call", {"name": "wipe", "arguments": {}})
    first = await r.mcp.handle(call, "admin")
    assert first["result"]["isError"] and "approval_required" in first["result"]["content"][0]["text"]
    (pending,) = r.approvals.pending()
    assert pending.tool == "wipe" and pending.caller == "admin"
    r.approvals.decide(pending.id, True, by="root")
    done = await r.mcp.handle(call, "admin")
    assert done["result"]["isError"] is False
    again = await r.mcp.handle(call, "admin")  # approval was single-use
    assert again["result"]["isError"]


async def test_max_permission_caps_even_with_approval():
    r = rt()  # default cap: write
    call = rpc("tools/call", {"name": "wipe", "arguments": {}})
    out = await r.mcp.handle(call, "admin")
    assert "permission_denied" in out["result"]["content"][0]["text"]


def test_approval_store_rules():
    t = {"now": 0.0}
    st = ApprovalStore(ttl_s=10, max_items=2, clock=lambda: t["now"])
    a = st.request("k", "wipe", {"x": 1})
    assert st.request("k", "wipe", {"x": 1}) is a  # same call reuses the pending one
    assert not st.consume("k", "wipe", {"x": 1})  # not approved yet
    st.decide(a.id, False, "root")
    with pytest.raises(ValueError):
        st.decide(a.id, True, "root")  # already decided
    b = st.request("k", "wipe", {"x": 2})
    st.decide(b.id, True, "root")
    assert not st.consume("other", "wipe", {"x": 2})  # bound to caller
    assert not st.consume("k", "wipe", {"x": 3})  # and to exact arguments
    assert st.consume("k", "wipe", {"x": 2}) and not st.consume("k", "wipe", {"x": 2})
    t["now"] = 100.0
    assert st.get(a.id) is None  # expired


def test_config_defaults_and_validation():
    r = build_runtime({})
    assert r.allowed_for("anyone") == []  # deny by default
    assert [t.name for t in r.registry.visible(["*"])] == ["calculator"]
    with pytest.raises(ValueError):
        build_runtime({"max_permission": "root"})
    limited = build_runtime({"rate_per_minute": {"calculator": 5}})
    assert limited.registry.get("calculator").rate_per_minute == 5


def test_stdio_server_end_to_end(tmp_path):
    import json
    import subprocess
    import sys

    cfg = tmp_path / "tools.yml"
    cfg.write_text("grants:\n  stdio: [calculator]\n")
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "calculator", "arguments": {"expression": "9*9"}}},
    ]  # fmt: skip
    proc = subprocess.run(
        [sys.executable, "-m", "tools"], input="\n".join(map(json.dumps, msgs)) + "\n",
        capture_output=True, text=True, timeout=30,
        env={**__import__("os").environ, "COREAI_TOOLS_FILE": str(cfg)},
    )  # fmt: skip
    replies = [json.loads(line) for line in proc.stdout.splitlines()]
    assert [r["id"] for r in replies] == [1, 2, 3]  # the notification got no reply
    assert replies[1]["result"]["tools"][0]["name"] == "calculator"
    assert "81" in replies[2]["result"]["content"][0]["text"]


def test_webhook_config_needs_secret(monkeypatch):
    monkeypatch.delenv("TOOLS_WEBHOOK_SECRET", raising=False)
    with pytest.raises(ValueError):
        build_runtime({"approval_webhook": {"url": "http://h/x"}})
    monkeypatch.setenv("TOOLS_WEBHOOK_SECRET", "k")
    assert build_runtime({"approval_webhook": {"url": "http://h/x"}}).approvals.on_request


def test_extra_tools_register_only_when_configured(tmp_path):
    cfg = {
        "web_search": {"url": "https://s.example/search"},
        "databases": {"app": str(tmp_path / "a.db")},
        "shell": {"allowed_commands": ["echo"], "cwd": str(tmp_path)},
    }
    names = [t.name for t in build_runtime(cfg).registry.visible(["*"])]
    assert names == ["calculator", "db_query", "shell_run", "web_search"]
    assert [t.name for t in build_runtime({}).registry.visible(["*"])] == ["calculator"]
