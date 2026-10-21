import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.local_models_api import router
from tests.gguf_helpers import write_gguf
from tests.test_managed_provider import KV, make


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("SECRET_KEY", "local-api-test-secret")
    import api.server as server
    from api.auth import create_access_token

    prov, sup, _ = make(tmp_path)
    monkeypatch.setitem(server.app_state, "providers", {"gguf": prov})
    app = FastAPI()
    app.include_router(router)

    def h(scopes):
        return {"Authorization": "Bearer " + create_access_token("u", scopes)}

    return TestClient(app), h, prov, sup, tmp_path


def test_requires_admin(env):
    c, h, *_ = env
    assert c.get("/v1/local/models", headers=h(["completions:read"])).status_code == 403


def test_list_inspect(env):
    c, h, prov, sup, tmp = env
    write_gguf(tmp / "second.gguf", KV)  # appears after rescan
    data = c.get("/v1/local/models", headers=h(["admin"])).json()["data"]
    assert {m["id"] for m in data} == {"tiny", "second"}
    assert all("path" not in m and m["loaded"] is False for m in data)
    r = c.get("/v1/local/models/tiny", headers=h(["admin"]))
    assert r.json()["quantization"] == "Q4_K_M"
    assert c.get("/v1/local/models/zzz", headers=h(["admin"])).status_code == 404


def test_delete_and_unload(env):
    c, h, prov, sup, tmp = env
    assert c.delete("/v1/local/models/tiny", headers=h(["admin"])).status_code == 200
    assert not (tmp / "tiny.gguf").exists() and "tiny" not in prov.models
    assert c.delete("/v1/local/models/tiny", headers=h(["admin"])).status_code == 404


def test_pull_bad_spec(env, monkeypatch):
    c, h, *_ = env
    import localrt.download as d

    monkeypatch.setattr(d, "hf_files", lambda repo, client=None: {"a.md": {}})
    r = c.post("/v1/local/models/pull", json={"model": "o/r:Q4"}, headers=h(["admin"]))
    assert r.status_code == 400


def test_disabled_runtime_is_404(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "local-api-test-secret")
    import api.server as server
    from api.auth import create_access_token

    monkeypatch.setitem(server.app_state, "providers", {})
    app = FastAPI()
    app.include_router(router)
    h = {"Authorization": "Bearer " + create_access_token("u", ["admin"])}
    assert TestClient(app).get("/v1/local/models", headers=h).status_code == 404
