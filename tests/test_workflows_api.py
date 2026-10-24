"""Workflow config and HTTP endpoints."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from harness.runtime import build_runtime
from providers.base import CompletionResponse
from tools import Permission, Tool
from tools.config import build_runtime as build_tools

WF = {
    "daily": {
        "max_cost_usd": 1.0,
        "steps": [
            {"id": "calc", "tool": "calculator", "args": {"expression": "${input}"}},
            {"id": "say", "prompt": "Explain: ${calc}"},
        ],
    },
    "danger": {"steps": [{"id": "w", "tool": "wipe"}]},
}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "wf-api-test-secret")
    import api.server as server
    from api.auth import create_access_token
    from api.runs_api import router

    tools = build_tools({"grants": {"default": ["calculator"], "admin": ["*"]}})
    tools.registry.register(
        Tool("wipe", "w", {"type": "object"}, lambda: "wiped", permission=Permission.DESTRUCTIVE)
    )  # fmt: skip

    async def route(req):
        return CompletionResponse("because math", "m", "p", 1, 1, 0.0, 1.0)

    rt = build_runtime(
        {"db": ":memory:", "agents": {}, "workflows": WF}, route, tools.registry
    )
    monkeypatch.setitem(server.app_state, "harness", rt)
    monkeypatch.setitem(server.app_state, "tools", tools)
    app = FastAPI()
    app.include_router(router)
    c = TestClient(app, base_url="http://testserver")

    def as_(sub, scopes):
        return {"Authorization": "Bearer " + create_access_token(sub, scopes)}

    return c, as_


RUN = ["runs:read", "runs:write"]


def test_list_and_run(client):
    c, as_ = client
    h = as_("u1", RUN)
    names = [w["name"] for w in c.get("/v1/workflows", headers=h).json()["data"]]
    assert names == ["danger", "daily"] or sorted(names) == ["daily", "danger"]
    out = c.post("/v1/workflows/daily/run", json={"input": "2+3"}, headers=h).json()
    assert out["status"] == "completed" and out["result"] == "because math"
    assert "5" in out["steps"][0]["output"]


def test_tool_not_granted_and_unknown(client):
    c, as_ = client
    h = as_("u1", RUN)
    assert c.post("/v1/workflows/danger/run", json={}, headers=h).status_code == 403
    assert c.post("/v1/workflows/nope/run", json={}, headers=h).status_code == 404


def test_approval_needs_admin_and_valid_step(client):
    c, as_ = client
    admin = as_("admin", RUN + ["admin"])
    r = c.post("/v1/workflows/danger/run", json={}, headers=admin).json()
    assert r["status"] == "needs_approval"
    r = c.post(
        "/v1/workflows/danger/run", json={"approved": ["w"]}, headers=admin
    ).json()
    assert r["status"] == "completed"
    assert (
        c.post(
            "/v1/workflows/danger/run", json={"approved": ["zz"]}, headers=admin
        ).status_code
        == 422
    )
    user = as_("u1", RUN)
    assert (
        c.post(
            "/v1/workflows/daily/run", json={"approved": ["calc"]}, headers=user
        ).status_code
        == 403
    )


def test_bad_workflow_config_reports_problems():
    with pytest.raises(ValueError, match="workflow 'x'"):
        build_runtime({"db": ":memory:", "workflows": {"x": {"steps": []}}}, None, None)


# ---- run history ---------------------------------------------------------


def test_runs_are_recorded_and_scoped_to_the_caller(client):
    c, as_ = client
    h = as_("u1", RUN)
    out = c.post("/v1/workflows/daily/run", json={"input": "2+3"}, headers=h).json()
    rid = out["run_id"]
    rows = c.get("/v1/workflows/daily/runs", headers=h).json()["data"]
    assert rows[0]["id"] == rid
    assert rows[0]["status"] == "completed" and rows[0]["caller"] == "u1"
    assert "steps" not in rows[0]  # list is the summary only
    detail = c.get(f"/v1/workflows/daily/runs/{rid}", headers=h).json()
    assert detail["steps"][0]["id"] == "calc"
    assert "5" in detail["steps"][0]["output"] and detail["input"] == "2+3"
    # another caller sees neither the row nor the detail; an admin sees both
    other = as_("u2", RUN)
    assert c.get("/v1/workflows/daily/runs", headers=other).json()["data"] == []
    assert c.get(f"/v1/workflows/daily/runs/{rid}", headers=other).status_code == 404
    admin = as_("admin", RUN + ["admin"])
    assert len(c.get("/v1/workflows/daily/runs", headers=admin).json()["data"]) == 1
    assert c.get(f"/v1/workflows/daily/runs/{rid}", headers=admin).status_code == 200


def test_history_records_approvals_and_failures_too(client):
    c, as_ = client
    admin = as_("admin", RUN + ["admin"])
    r = c.post("/v1/workflows/danger/run", json={}, headers=admin).json()
    assert r["status"] == "needs_approval"
    rid = r["run_id"]
    detail = c.get(f"/v1/workflows/danger/runs/{rid}", headers=admin).json()
    assert detail["status"] == "needs_approval"
    assert detail["failed_step"] == "w"
    assert c.post("/v1/workflows/danger/run", json={"approved": ["w"]}, headers=admin)
    rows = c.get("/v1/workflows/danger/runs", headers=admin).json()["data"]
    assert sorted(x["status"] for x in rows) == ["completed", "needs_approval"]


def test_history_unknown_names(client):
    c, as_ = client
    h = as_("u1", RUN)
    assert c.get("/v1/workflows/nope/runs", headers=h).status_code == 404
    assert c.get("/v1/workflows/daily/runs/zzz", headers=h).status_code == 404
    # a run id from another workflow does not leak
    out = c.post("/v1/workflows/daily/run", json={"input": "1"}, headers=h).json()
    assert (
        c.get(f"/v1/workflows/danger/runs/{out['run_id']}", headers=h).status_code
        == 404
    )
