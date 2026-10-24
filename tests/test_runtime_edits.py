"""Runtime routing-policy edits: store overlay, admin API, caips CLI."""

import io
import json
import os

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from coreai.policy import PolicyError, PolicyStore
from coreai.router import Router, RoutingConfig, RoutingStrategy
from tests.helpers import FakeClock, make_provider, request

GOOD = """
policies:
  default:
    strategy: cost_aware
  fast:
    strategy: fastest
"""


def write(path, text):
    path.write_text(text)
    st = os.stat(path)
    os.utime(path, (st.st_atime, st.st_mtime + 5))  # mtime must change


def make_store(tmp_path, text=GOOD):
    f = tmp_path / "routing.yml"
    f.write_text(text)
    return PolicyStore(str(f), clock=FakeClock()), f


# ------------------------------------------------------------------ store


def test_set_adds_a_policy_and_overrides_a_file_policy(tmp_path):
    store, _ = make_store(tmp_path)
    p = store.set_policy("cheap", {"strategy": "cheapest", "deny": ["grok"]})
    assert p.strategy == "cheapest" and p.deny == frozenset({"grok"})
    store.set_policy(
        "fast", {"strategy": "balanced", "weights": {"cost": 1, "latency": 3}}
    )
    cur = store.current()
    assert "cheap" in cur.names()
    assert cur.get("fast").strategy == "balanced"
    assert cur.get("fast").latency_weight == pytest.approx(0.75)
    assert store.runtime_names() == {"edited": ["cheap", "fast"], "removed": []}


def test_set_rejects_bad_bodies_and_names_without_touching_active(tmp_path):
    store, _ = make_store(tmp_path)
    before = store.current().names()
    with pytest.raises(PolicyError) as e:
        store.set_policy("bad", {"strategy": "magic", "extra": 1})
    assert len(e.value.problems) == 2
    with pytest.raises(PolicyError, match="policy name"):
        store.set_policy("not a name!", {})
    with pytest.raises(PolicyError, match="mapping"):
        store.set_policy("ok-name", ["not", "a", "map"])
    assert store.current().names() == before


def test_edits_survive_a_file_reload_and_bad_files(tmp_path):
    store, f = make_store(tmp_path)
    store.set_policy("cheap", {"strategy": "cheapest"})
    write(f, GOOD + "  extra:\n    strategy: round_robin\n")
    store._load()
    cur = store.current()
    assert cur.get("cheap").strategy == "cheapest"  # runtime edit still wins
    assert "extra" in cur.names()
    write(f, "policies:\n  default:\n    strategy: nonsense\n")
    store._load()
    assert store.current().get("cheap") is not None  # bad file keeps everything


def test_remove_covers_file_policies_runtime_edits_and_missing(tmp_path):
    store, _ = make_store(tmp_path)
    store.set_policy("cheap", {"strategy": "cheapest"})
    assert store.remove_policy("cheap") is True  # runtime edit only
    assert store.remove_policy("fast") is True  # file policy
    assert store.remove_policy("fast") is False  # already gone
    assert store.remove_policy("ghost") is False
    assert store.current().names() == ["default"]
    assert store.runtime_names() == {"edited": [], "removed": ["fast"]}
    # Setting again undoes a removal.
    store.set_policy("fast", {"strategy": "fastest"})
    assert store.current().get("fast").strategy == "fastest"
    assert store.runtime_names()["removed"] == []


@pytest.mark.asyncio
async def test_router_routes_with_a_runtime_policy(tmp_path):
    store, _ = make_store(
        tmp_path,
        """
policies:
  default:
    strategy: cheapest
""",
    )
    providers = {
        "a": make_provider("a", cost=0.003),
        "b": make_provider("b", cost=0.001),
    }
    r = Router(
        providers,
        RoutingConfig(strategy=RoutingStrategy.FALLBACK, enable_cache=False),
        policies=store,
    )
    resp = await r.route(request())
    assert resp.provider == "b"
    store.set_policy(
        "default", {"strategy": "cheapest", "deny": ["b"]}
    )  # cheapest is now a
    resp = await r.route(request())
    assert resp.provider == "a"
    providers["b"].complete.assert_called_once()


# -------------------------------------------------------------------- API


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "runtime-edit-secret")
    import api.server as server
    from api.auth import create_access_token
    from api.routes import router as routes_router

    providers = {
        "openai": make_provider("openai"),
        "anthropic": make_provider("anthropic"),
    }
    f = tmp_path / "routing.yml"
    f.write_text(GOOD)
    r = Router(
        providers,
        RoutingConfig(strategy=RoutingStrategy.FALLBACK, enable_cache=False),
        policies=PolicyStore(str(f), clock=FakeClock()),
    )
    monkeypatch.setitem(server.app_state, "router", r)
    app = FastAPI()
    app.include_router(routes_router)
    c = TestClient(app, base_url="http://testserver")

    def h(*scopes):
        return {
            "Authorization": "Bearer " + create_access_token("user:t", list(scopes))
        }

    return c, h, r.policies


def test_api_set_lists_and_removes(api):
    c, h, store = api
    assert (
        c.put(
            "/v1/admin/policies/cheap",
            json={"strategy": "cheapest", "deny": ["grok"]},
            headers=h("admin"),
        ).status_code
        == 200
    )
    body = c.get("/v1/admin/policies", headers=h("admin")).json()
    names = [p["name"] for p in body["policies"]]
    assert "cheap" in names and "default" in names
    assert body["runtime_edits"] == {"edited": ["cheap"], "removed": []}
    assert (
        c.delete("/v1/admin/policies/cheap", headers=h("admin")).json()["removed"]
        is True
    )
    assert "cheap" not in store.current().names()


def test_api_edit_is_admin_only_and_audited(api):
    c, h, _ = api
    body = {"strategy": "cheapest"}
    assert c.put("/v1/admin/policies/x", json=body).status_code in (401, 403)
    assert (
        c.put(
            "/v1/admin/policies/x", json=body, headers=h("completions:read")
        ).status_code
        == 403
    )
    c.put("/v1/admin/policies/x", json=body, headers=h("admin"))
    c.delete("/v1/admin/policies/x", headers=h("admin"))
    actions = [
        e["action"]
        for e in c.get("/v1/admin/audit", headers=h("admin")).json()["events"]
    ]
    assert "policy.set" in actions and "policy.remove" in actions


def test_api_reports_every_problem_and_keeps_active(api):
    c, h, store = api
    before = store.current().names()
    r = c.put(
        "/v1/admin/policies/bad",
        json={
            "strategy": "cheapest",
            "weights": {"cost": -1, "latency": 1},
            "bogus": 1,
        },
        headers=h("admin"),
    )
    assert r.status_code == 422
    assert len(r.json()["detail"]["problems"]) == 2
    assert store.current().names() == before
    assert c.delete("/v1/admin/policies/ghost", headers=h("admin")).status_code == 404


def test_api_without_policy_file_is_409(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "runtime-edit-secret-2")
    import api.server as server
    from api.auth import create_access_token
    from api.routes import router as routes_router

    monkeypatch.setitem(
        server.app_state,
        "router",
        Router(
            {}, RoutingConfig(strategy=RoutingStrategy.FALLBACK, enable_cache=False)
        ),
    )
    app = FastAPI()
    app.include_router(routes_router)
    c = TestClient(app, base_url="http://testserver")
    h = {"Authorization": "Bearer " + create_access_token("user:t", ["admin"])}
    assert c.put("/v1/admin/policies/x", json={}, headers=h).status_code == 409
    assert c.delete("/v1/admin/policies/x", headers=h).status_code == 409


# -------------------------------------------------------------------- CLI
# (same fake-transport harness as tests/test_caips_cli.py)


class Server:
    def __init__(self, table=None):
        self.table, self.seen = table or {}, []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content) if req.content else None
        self.seen.append((req.method, req.url.path, body))
        status, data = self.table.get((req.method, req.url.path), (200, {"ok": True}))
        return httpx.Response(status, json=data)


def run(argv, srv=None):
    from caips_cli.cli import main

    srv = srv or Server()
    out, err = io.StringIO(), io.StringIO()
    code = main(argv, transport=httpx.MockTransport(srv), out=out, err=err)
    return code, out.getvalue(), err.getvalue(), srv


def test_cli_policy_set_from_flags():
    code, out, _, srv = run(
        [
            "policy-set",
            "cheap",
            "--strategy",
            "cheapest",
            "--weights",
            "0.7,0.3",
            "--ceiling",
            "800",
            "--hedge",
            "2500",
            "--chain",
            "openai, anthropic",
            "--allow",
            "openai,anthropic",
            "--deny",
            "grok",
            "--model",
            "gpt-x",
            "--fallback-model",
            "claude-x",
        ]
    )
    assert code == 0
    method, path, body = srv.seen[0]
    assert (method, path) == ("PUT", "/v1/admin/policies/cheap")
    assert body == {
        "strategy": "cheapest",
        "weights": {"cost": 0.7, "latency": 0.3},
        "latency_ceiling_ms": 800.0,
        "hedge_after_ms": 2500.0,
        "fallback_chain": ["openai", "anthropic"],
        "allow": ["openai", "anthropic"],
        "deny": ["grok"],
        "model": "gpt-x",
        "fallback_model": "claude-x",
    }


def test_cli_policy_set_from_file_and_flag_errors(tmp_path):
    f = tmp_path / "p.yml"
    f.write_text("strategy: fastest\nhedge_after_ms: 500\n")
    code, _, _, srv = run(["policy-set", "fast", "--file", str(f)])
    assert code == 0 and srv.seen[0][2] == {
        "strategy": "fastest",
        "hedge_after_ms": 500,
    }
    for bad in (["--weights", "0.7"], ["--weights", "a,b"], ["--ceiling", "low"]):
        code, _, err, _ = run(["policy-set", "x"] + bad)
        assert code == 2 and err.startswith("caips:")
    not_map = tmp_path / "bad.yml"
    not_map.write_text("- just\n- a\n- list\n")
    assert run(["policy-set", "x", "--file", str(not_map)])[0] == 2


def test_cli_policy_rm_and_workflow_history():
    code, _, _, srv = run(["policy-rm", "fast route"])
    assert code == 0 and srv.seen[0][:2] == ("DELETE", "/v1/admin/policies/fast route")
    for argv, path in (
        (["workflows", "runs", "daily"], "/v1/workflows/daily/runs"),
        (["workflows", "run-detail", "daily", "r1"], "/v1/workflows/daily/runs/r1"),
    ):
        code, _, _, srv = run(argv)
        assert code == 0
        assert srv.seen[0][:2] == ("GET", path)
