import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.memory_api import router
from memory import MemoryStore


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "memory-api-test-secret")
    import api.server as server
    from api.auth import create_access_token

    monkeypatch.setitem(server.app_state, "memory", MemoryStore(":memory:"))
    app = FastAPI()
    app.include_router(router)

    def h(user, scopes=("memory:read", "memory:write")):
        return {"Authorization": "Bearer " + create_access_token(user, list(scopes))}

    return TestClient(app), h


def test_write_read_search_forget(env):
    c, h = env
    assert (
        c.put(
            "/v1/memory/pet", json={"value": "cat named Miso"}, headers=h("a")
        ).status_code
        == 200
    )
    assert c.get("/v1/memory/pet", headers=h("a")).json()["value"] == "cat named Miso"
    assert len(c.get("/v1/memory", headers=h("a")).json()["data"]) == 1
    hits = c.get("/v1/memory/search", params={"q": "cat"}, headers=h("a")).json()[
        "data"
    ]
    assert hits and hits[0]["key"] == "pet"
    assert c.delete("/v1/memory/pet", headers=h("a")).json() == {"deleted": 1}
    assert c.get("/v1/memory/pet", headers=h("a")).status_code == 404


def test_owners_are_isolated(env):
    c, h = env
    c.put("/v1/memory/k", json={"value": "secret"}, headers=h("a"))
    assert c.get("/v1/memory/k", headers=h("b")).status_code == 404
    assert c.get("/v1/memory", headers=h("b")).json()["data"] == []
    assert c.delete("/v1/memory", headers=h("b")).json() == {"deleted": 0}
    assert c.get("/v1/memory/k", headers=h("a")).status_code == 200


def test_scopes_enforced(env):
    c, h = env
    ro = h("a", ("memory:read",))
    assert c.put("/v1/memory/k", json={"value": "v"}, headers=ro).status_code == 403
    assert c.get("/v1/memory", headers=h("a", ("completions:read",))).status_code == 403
    assert c.get("/v1/memory").status_code in (401, 403)


def test_forget_namespace_and_oversize(env):
    c, h = env
    c.put("/v1/memory/a", json={"value": "1"}, params={"ns": "x"}, headers=h("a"))
    c.put("/v1/memory/b", json={"value": "2"}, headers=h("a"))
    assert c.delete("/v1/memory", params={"ns": "x"}, headers=h("a")).json() == {
        "deleted": 1
    }
    assert len(c.get("/v1/memory", headers=h("a")).json()["data"]) == 1
    big = c.put("/v1/memory/big", json={"value": "x" * 9000}, headers=h("a"))
    assert big.status_code == 422
