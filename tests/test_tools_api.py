"""Phase 6: tools over HTTP: /mcp, /v1/tools, approvals, run_tools."""

import json
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from providers.base import CompletionResponse
from tests.helpers import make_provider, make_router
from tools import Permission, Tool
from tools.config import build_runtime

BODY = {"model": "a-model", "run_tools": True,
        "messages": [{"role": "user", "content": "what is 6*7?"}]}  # fmt: skip


def tool_reply(calls=None, text="the answer is 42"):
    return CompletionResponse(
        content="" if calls else text, model="a-model", provider="a", input_tokens=5,
        output_tokens=2, cost_usd=0.01, latency_ms=1.0,
        metadata={"tool_calls": calls} if calls else {},
    )  # fmt: skip


def call(name, args, cid="c1"):
    return {"id": cid, "name": name, "arguments": json.dumps(args)}


@pytest.fixture
def make_client(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "tools-api-test-secret")
    import api.server as server
    from api.auth import create_access_token
    from api.openai_compat import router as compat
    from api.routes import router as api_router
    from api.tools_api import router as tools_router

    started = []

    def build(
        router,
        runtime,
        subject="u",
        scopes=("completions:write", "tools:read", "tools:call"),
    ):
        app = FastAPI()
        for r in (api_router, compat, tools_router):
            app.include_router(r)
        for p in (
            patch.object(server, "get_router", return_value=router),
            patch.dict(server.app_state, {"tools": runtime}),
        ):
            p.start()
            started.append(p)
        c = TestClient(app, base_url="http://testserver")
        c.headers["Authorization"] = "Bearer " + create_access_token(
            subject, list(scopes)
        )
        return c

    yield build
    for p in started:
        p.stop()


def runtime(**extra):
    rt = build_runtime({"grants": {"u": ["calculator"], "boss": ["*"]}, **extra})
    rt.registry.register(
        Tool(
            "wipe",
            "w",
            {"type": "object"},
            lambda: "wiped",
            permission=Permission.DESTRUCTIVE,
        )
    )
    return rt


def test_run_tools_executes_and_returns_final_answer(make_client):
    p = make_provider("a", behaviour=[
        tool_reply([call("calculator", {"expression": "6*7"})]), tool_reply()
    ])  # fmt: skip
    c = make_client(make_router({"a": p}), runtime())
    r = c.post("/v1/chat/completions", json=BODY)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["choices"][0]["message"]["content"] == "the answer is 42"
    assert j["caips_tool_steps"] == [{"tool": "calculator", "ok": True, "error": None,
                                      "duration_ms": j["caips_tool_steps"][0]["duration_ms"]}]  # fmt: skip
    assert j["usage"]["prompt_tokens"] == 10  # both model calls counted


def test_run_tools_refused_without_server_side_grant(make_client):
    c = make_client(make_router({"a": make_provider("a")}), runtime(), subject="nobody")
    r = c.post("/v1/chat/completions", json=BODY)
    assert r.status_code == 400 and r.json()["error"]["code"] == "tools_not_enabled"
    c2 = make_client(make_router({"a": make_provider("a")}), None)
    assert c2.post("/v1/chat/completions", json=BODY).status_code == 400


def test_destructive_call_returns_409_then_succeeds_after_approval(make_client):
    rt = runtime(max_permission="destructive")
    script = [
        tool_reply([call("wipe", {})]),
        tool_reply([call("wipe", {})]),
        tool_reply(text="wiped it"),
    ]
    c = make_client(make_router({"a": make_provider("a", behaviour=script)}), rt, subject="boss",
                    scopes=("completions:write", "admin", "tools:read", "tools:call"))  # fmt: skip
    r = c.post("/v1/chat/completions", json=BODY)
    assert r.status_code == 409 and r.json()["error"]["code"] == "approval_required"
    (pending,) = c.get("/v1/tools/approvals").json()["data"]
    assert pending["tool"] == "wipe"
    assert (
        c.post(
            f"/v1/tools/approvals/{pending['id']}", json={"approve": True}
        ).status_code
        == 200
    )
    again = c.post("/v1/chat/completions", json=BODY)
    assert (
        again.status_code == 200
        and again.json()["choices"][0]["message"]["content"] == "wiped it"
    )


def test_approval_endpoints_need_admin(make_client):
    c = make_client(make_router({"a": make_provider("a")}), runtime())
    assert c.get("/v1/tools/approvals").status_code == 403
    assert c.post("/v1/tools/approvals/x", json={"approve": True}).status_code == 403


def test_list_tools_and_mcp_over_http(make_client):
    c = make_client(make_router({"a": make_provider("a")}), runtime())
    names = [t["name"] for t in c.get("/v1/tools").json()["data"]]
    assert names == ["calculator"]
    r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": "calculator", "arguments": {"expression": "2+3"}}})  # fmt: skip
    assert r.status_code == 200 and "5" in r.json()["result"]["content"][0]["text"]
    n = c.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert n.status_code == 202


def test_tools_disabled_returns_404_and_scopes_enforced(make_client):
    assert (
        make_client(make_router({"a": make_provider("a")}), None)
        .get("/v1/tools")
        .status_code
        == 404
    )
    c = make_client(
        make_router({"a": make_provider("a")}), runtime(), scopes=("completions:write",)
    )
    assert c.get("/v1/tools").status_code == 403


async def test_tool_calls_are_audited_without_argument_values():
    from api.server import tool_audit_hook
    from coreai.audit import AuditLog
    from tools import ToolError

    audit = AuditLog()
    rt = build_runtime({"grants": {"u": ["calculator"]}})
    rt.registry.on_call = tool_audit_hook
    with patch("api.server.get_audit", return_value=audit):
        await rt.registry.call(
            "calculator", {"expression": "2+2"}, allowed=["calculator"], caller="u"
        )
        with pytest.raises(ToolError):
            await rt.registry.call(
                "calculator",
                {"expression": "SECRET"},
                allowed=["calculator"],
                caller="u",
            )
    events = [e for e in audit._events if e["action"] == "tool.call"]
    assert [e["outcome"] for e in events] == ["ok", "invalid_arguments"]
    assert events[0]["actor"] == "u" and events[0]["target"] == "calculator"
    assert events[0]["details"]["arguments"] == ["expression"]
    assert "SECRET" not in json.dumps(events)
