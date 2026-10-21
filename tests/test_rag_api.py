import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.rag_api import router
from rag.store import VectorStore


async def fake_embed(texts):
    # tiny deterministic bag-of-letters embedding: "cat" and "dog" texts differ
    return [[t.lower().count(c) + 0.01 for c in "catdog"] for t in texts]


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "rag-api-test-secret")
    import api.server as server
    from api.auth import create_access_token

    monkeypatch.setitem(server.app_state, "rag", VectorStore(":memory:", fake_embed))
    app = FastAPI()
    app.include_router(router)

    def h(user, scopes=("rag:read", "rag:write")):
        return {"Authorization": "Bearer " + create_access_token(user, list(scopes))}

    return TestClient(app), h


def test_ingest_list_search_delete(env):
    c, h = env
    r = c.post(
        "/v1/rag/docs/documents",
        json={"source": "a.md", "text": "cat cat cat"},
        headers=h("u"),
    )
    assert r.status_code == 201 and r.json()["chunks"] >= 1
    c.post(
        "/v1/rag/docs/documents",
        json={"source": "b.md", "text": "dog dog dog"},
        headers=h("u"),
    )
    assert [
        d["source"]
        for d in c.get("/v1/rag/docs/documents", headers=h("u")).json()["data"]
    ] == ["a.md", "b.md"]
    hits = c.post(
        "/v1/rag/docs/search", json={"query": "cat", "k": 1}, headers=h("u")
    ).json()["data"]
    assert hits[0]["source"] == "a.md"
    assert c.delete("/v1/rag/docs/documents/a.md", headers=h("u")).status_code == 200
    assert c.delete("/v1/rag/docs/documents/a.md", headers=h("u")).status_code == 404


def test_isolation_and_scopes(env):
    c, h = env
    c.post(
        "/v1/rag/docs/documents", json={"source": "a", "text": "cat"}, headers=h("u")
    )
    assert c.get("/v1/rag/docs/documents", headers=h("other")).json()["data"] == []
    assert (
        c.post("/v1/rag/docs/search", json={"query": "cat"}, headers=h("other")).json()[
            "data"
        ]
        == []
    )
    ro = h("u", ("rag:read",))
    assert (
        c.post(
            "/v1/rag/docs/documents", json={"source": "x", "text": "t"}, headers=ro
        ).status_code
        == 403
    )
    assert c.delete("/v1/rag/docs/documents/a", headers=ro).status_code == 403


def test_validation(env):
    c, h = env
    assert (
        c.post(
            "/v1/rag/docs/documents", json={"source": "", "text": "t"}, headers=h("u")
        ).status_code
        == 422
    )
    assert (
        c.post(
            "/v1/rag/docs/search", json={"query": "x", "k": 99}, headers=h("u")
        ).status_code
        == 422
    )
