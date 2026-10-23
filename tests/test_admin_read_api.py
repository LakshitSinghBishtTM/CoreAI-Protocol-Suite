"""Read-only admin endpoints behind `caips budgets` and `caips policies`."""

import pytest
from fastapi.testclient import TestClient

from coreai.budgets import BudgetLimits, BudgetManager


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "admin-read-secret")
    import api.server as server

    old = server.app_state.get("budgets")
    yield server
    server.app_state["budgets"] = old


def token(*scopes):
    from api.auth import create_access_token

    return {"Authorization": f"Bearer {create_access_token('user:t', list(scopes))}"}


@pytest.mark.parametrize("path", ["/v1/admin/budgets", "/v1/admin/policies"])
def test_endpoints_are_admin_only(server, path):
    c = TestClient(server.app)
    assert c.get(path).status_code in (401, 403)
    assert c.get(path, headers=token("completions:read")).status_code == 403


def test_budgets_disabled_without_manager(server):
    server.app_state["budgets"] = None
    r = TestClient(server.app).get("/v1/admin/budgets", headers=token("admin"))
    assert r.status_code == 200 and r.json() == {"enabled": False}


def test_budgets_report_limits_and_usage(server):
    mgr = BudgetManager(default=BudgetLimits(max_requests=10, window_seconds=60))
    server.app_state["budgets"] = mgr
    r = TestClient(server.app).get(
        "/v1/admin/budgets?subject=user:bob", headers=token("admin")
    )
    body = r.json()
    assert body["enabled"] and body["subject"] == "user:bob"
    assert body["status"]["limits"]["requests"] == 10
    assert body["status"]["used"]["requests"] == 0
