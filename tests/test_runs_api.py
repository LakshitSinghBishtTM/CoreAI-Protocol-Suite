"""Phase 7: run API (start, ownership, approvals, kill switch, stream)."""

import json
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from harness.runtime import build_runtime
from providers.base import CompletionResponse
from tools import Permission, Tool
from tools.config import build_runtime as build_tools

RUN = ["runs:read", "runs:write"]


def resp(text="", calls=None):
    return CompletionResponse(
        text, "m", "p", 1, 1, 0.0, 1.0, metadata={"tool_calls": calls} if calls else {}
    )


def wipe_call():
    return {"id": "c1", "name": "wipe", "arguments": "{}"}


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "runs-api-test-secret")
    import api.server as server
    from api.auth import create_access_token
    from api.runs_api import router

    def build(*script):
        tools = build_tools(
            {"grants": {"default": ["calculator"], "admin": ["*"], "root": ["*"]}}
        )
        tools.registry.register(
            Tool(
                "wipe",
                "w",
                {"type": "object"},
                lambda: "wiped",
                permission=Permission.DESTRUCTIVE,
            )
        )
        items = list(script)

        async def route(req):
            return items.pop(0) if len(items) > 1 else items[0]

        rt = build_runtime(
            {
                "db": ":memory:",
                "agents": {
                    "calc": {"system_prompt": "s", "allowed_tools": ["calculator"]},
                    "danger": {"system_prompt": "s", "allowed_tools": ["wipe"]},
                },
            },
            route,
            tools.registry,
        )
        monkeypatch.setitem(server.app_state, "harness", rt)
        monkeypatch.setitem(server.app_state, "tools", tools)
        app = FastAPI()
        app.include_router(router)
        c = TestClient(app, base_url="http://testserver")
        c.__enter__()  # keep one event loop alive so background runs finish

        def as_(sub, scopes):
            return {"Authorization": "Bearer " + create_access_token(sub, scopes)}

        return c, as_, rt

    clients = []

    def tracked(*script):
        out = build(*script)
        clients.append(out[0])
        return out

    yield tracked
    for c in clients:
        c.__exit__(None, None, None)


def wait(
    c, h, rid, want=("completed", "failed", "cancelled", "waiting_approval", "paused")
):
    for _ in range(100):
        r = c.get(f"/v1/runs/{rid}", headers=h).json()
        if r["status"] in want:
            return r
        time.sleep(0.02)
    raise AssertionError(r)


def test_start_status_events_and_ownership(api):
    c, as_, _ = api(resp("hello"))
    alice, bob = as_("alice", RUN), as_("bob", RUN)
    r = c.post("/v1/runs", json={"agent": "calc", "task": "hi"}, headers=alice)
    assert r.status_code == 202
    rid = r.json()["id"]
    assert wait(c, alice, rid)["result"] == "hello"
    ev = c.get(f"/v1/runs/{rid}/events", headers=alice).json()["data"]
    assert ev[0]["type"] == "run_started" and ev[-1]["type"] == "run_finished"
    assert [x["id"] for x in c.get("/v1/runs", headers=alice).json()["data"]] == [rid]
    assert c.get("/v1/runs", headers=bob).json()["data"] == []
    assert c.get(f"/v1/runs/{rid}", headers=bob).status_code == 404
    assert (
        c.get("/v1/runs", headers=as_("root", ["admin"])).json()["data"][0]["id"] == rid
    )


def test_unknown_agent_ungranted_tools_and_scope(api):
    c, as_, _ = api(resp("x"))
    h = as_("alice", RUN)
    assert (
        c.post("/v1/runs", json={"agent": "nope", "task": "t"}, headers=h).status_code
        == 404
    )
    r = c.post("/v1/runs", json={"agent": "danger", "task": "t"}, headers=h)
    assert r.status_code == 403 and "wipe" in r.json()["detail"]
    assert (
        c.post(
            "/v1/runs",
            json={"agent": "calc", "task": "t"},
            headers=as_("alice", ["runs:read"]),
        ).status_code
        == 403
    )


def test_approval_needs_admin_and_resumes(api):
    c, as_, _ = api(resp(calls=[wipe_call()]), resp("done"))
    admin = as_("root", ["admin"])
    rid = c.post(
        "/v1/runs", json={"agent": "danger", "task": "t"}, headers=admin
    ).json()["id"]
    assert wait(c, admin, rid)["status"] == "waiting_approval"
    ev = c.get(f"/v1/runs/{rid}/events", headers=admin).json()["data"]
    assert ev[-1]["type"] == "approval_requested"
    assert c.post(f"/v1/runs/{rid}/resume", json={}, headers=admin).status_code == 400
    r = c.post(f"/v1/runs/{rid}/resume", json={"approve": True}, headers=admin)
    assert r.status_code == 200
    assert wait(c, admin, rid, ("completed",))["result"] == "done"
    assert c.post(f"/v1/runs/{rid}/resume", json={}, headers=admin).status_code == 409


def test_non_admin_cannot_approve_own_destructive_call(api):
    c, as_, rt = api(resp(calls=[wipe_call()]), resp("done"))
    admin = as_("root", ["admin"])
    rid = c.post(
        "/v1/runs", json={"agent": "danger", "task": "t"}, headers=admin
    ).json()["id"]
    wait(c, admin, rid)
    rt.runner.store.set(rid, owner="alice")
    alice = as_("alice", RUN)
    assert (
        c.post(
            f"/v1/runs/{rid}/resume", json={"approve": True}, headers=alice
        ).status_code
        == 403
    )
    assert c.post(f"/v1/runs/{rid}/cancel", headers=alice).json() == {"cancelled": True}
    assert wait(c, alice, rid)["status"] == "cancelled"


def test_kill_switch_is_admin_only_and_stops_waiting_runs(api):
    c, as_, _ = api(resp(calls=[wipe_call()]), resp("done"))
    admin = as_("root", ["admin"])
    rid = c.post(
        "/v1/runs", json={"agent": "danger", "task": "t"}, headers=admin
    ).json()["id"]
    assert wait(c, admin, rid)["status"] == "waiting_approval"
    assert (
        c.post("/v1/runs/kill", json={}, headers=as_("alice", RUN)).status_code == 403
    )
    assert (
        c.post("/v1/runs/kill", json={"owner": "nobody"}, headers=admin).json()[
            "killed"
        ]
        == 0
    )
    assert c.post("/v1/runs/kill", json={}, headers=admin).json()["killed"] == 1
    r = c.get(f"/v1/runs/{rid}", headers=admin).json()
    assert r["status"] == "cancelled" and r["stop_reason"] == "killed"


def test_sse_stream_ends_with_done(api):
    c, as_, _ = api(resp("ok"))
    h = as_("alice", RUN)
    rid = c.post("/v1/runs", json={"agent": "calc", "task": "t"}, headers=h).json()[
        "id"
    ]
    body = c.get(f"/v1/runs/{rid}/stream", headers=h).text
    lines = [x[6:] for x in body.split("\n\n") if x.startswith("data: ")]
    assert lines[-1] == "[DONE]" and json.loads(lines[0])["type"] == "run_started"
    assert json.loads(lines[-2])["type"] == "run_finished"


def test_disabled_without_config(api, monkeypatch):
    import api.server as server

    c, as_, _ = api(resp("x"))
    monkeypatch.setitem(server.app_state, "harness", None)
    assert c.get("/v1/runs", headers=as_("alice", RUN)).status_code == 404


async def test_gateway_route_checks_and_records_the_owner_budget(monkeypatch):
    from api import routes
    from api.runs_api import gateway_route

    seen = []
    monkeypatch.setattr(
        routes,
        "_budget_check",
        lambda ctx, model=None: seen.append(("check", ctx.subject)),
    )
    monkeypatch.setattr(
        routes,
        "_budget_record",
        lambda ctx, cost, tokens, model=None: seen.append(("rec", ctx.subject, tokens)),
    )
    monkeypatch.setattr(
        routes, "_ledger_record", lambda ctx, *a: seen.append(("ledger", ctx.subject))
    )

    class R:
        async def route(self, req):
            return CompletionResponse("x", "m", "p", 3, 4, 0.1, 1.0)

    out = await gateway_route(R())(
        SimpleNamespace(metadata={"owner": "alice"}, model=None)
    )
    assert out.content == "x"
    assert seen == [("check", "alice"), ("rec", "alice", 7), ("ledger", "alice")]
