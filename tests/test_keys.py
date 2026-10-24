import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from coreai import Router, keystore
from coreai.errors import RateLimited
from providers import load_providers
from providers.base import CompletionRequest, CompletionResponse, Message
from providers.pool import KeyPoolProvider


@pytest.fixture(autouse=True)
def store(monkeypatch, tmp_path):
    monkeypatch.setenv("SECRET_KEY", "keys-test-secret")
    monkeypatch.setenv("CAIPS_KEYSTORE", str(tmp_path / "keys.json"))
    for v in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "GROK_API_KEY",
        "DEEPSEEK_API_KEY",
        "QWEN_API_KEY",
    ):
        monkeypatch.delenv(v, raising=False)
    return tmp_path / "keys.json"


def test_encrypted_at_rest_and_masked(store):
    e = keystore.add("openai", "sk-secretvalue1234")
    assert "sk-secretvalue1234" not in store.read_text()
    assert oct(store.stat().st_mode & 0o777) == "0o600"
    assert e["key"] == "…1234" and "api_key" not in e
    assert "sk-secretvalue1234" not in str(keystore.list_entries())
    assert keystore.load_secrets()[0]["api_key"] == "sk-secretvalue1234"


def test_wrong_secret_is_skipped_not_guessed(monkeypatch):
    keystore.add("openai", "sk-abcdefgh1234")
    monkeypatch.setenv("SECRET_KEY", "different")
    assert keystore.load_secrets() == []
    assert keystore.list_entries()[0]["readable"] is False


@pytest.mark.parametrize(
    "args",
    [
        ("nope", "k"),
        ("openai", ""),
        ("openai", "k", "x", "http://evil"),  # native: no base_url
        ("custom", "k", "Bad Name", "http://h/v1"),
        ("custom", "k", "openai", "http://h/v1"),  # reserved
        ("custom", "k", "mine", ""),  # needs base_url
        ("custom", "k", "mine", "ftp://h"),
    ],
)
def test_validation(args):
    with pytest.raises(keystore.KeyStoreError):
        keystore.add(*args)


def test_duplicate_labels():
    keystore.add("openai", "k1", "work")
    with pytest.raises(keystore.KeyStoreError):
        keystore.add("openai", "k2", "work")
    assert keystore.add("openai", "k2")["label"] == "2"


def test_loader_builds_pool_local_custom_and_compat():
    keystore.add("openai", "sk-aaaaaaaa1111", "a")
    keystore.add("openai", "sk-bbbbbbbb2222", "b")
    keystore.add("qwen", "q-key-12345678")
    keystore.add("custom", "c-key-12345678", "mine", "http://h/v1", "m1")
    keystore.add("local", "", "ollama")
    r = load_providers(include_local_yml=False)
    assert isinstance(r["openai"], KeyPoolProvider) and len(r["openai"].members) == 2
    assert r["qwen"].name == "qwen" and "dashscope" in r["qwen"].base_url
    assert r["mine"].default_model == "m1"
    assert r["ollama"].is_local and r["ollama"].base_url.endswith(":11434/v1")


def test_env_key_joins_pool(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-envenvenv0000")
    keystore.add("openai", "sk-aaaaaaaa1111")
    p = load_providers(include_local_yml=False)["openai"]
    assert p.labels == ["env", "1"]


class _Fake:
    def __init__(self, fail=None):
        self.fail, self.calls = fail, 0
        self.default_model, self.api_key = "m", "k"
        self.total_requests = self.total_tokens = 0
        self.total_cost = 0.0

    async def complete(self, req):
        self.calls += 1
        if self.fail:
            raise self.fail
        return CompletionResponse(
            content="ok",
            model="m",
            provider="x",
            input_tokens=1,
            output_tokens=1,
            cost_usd=0,
            latency_ms=1,
        )


def _req():
    return CompletionRequest(messages=[Message(role="user", content="hi")])


def test_pool_rotates_and_skips_exhausted_key():
    bad, good = _Fake(RateLimited("slow", retry_after=1)), _Fake()
    now = [0.0]
    pool = KeyPoolProvider("x", [bad, good], ["bad", "good"], clock=lambda: now[0])
    for _ in range(4):
        assert asyncio.run(pool.complete(_req())).content == "ok"
    assert bad.calls == 1  # parked after its first failure
    assert pool.key_states()[0]["parked"] is True
    now[0] = 61
    asyncio.run(pool.complete(_req()))
    asyncio.run(pool.complete(_req()))
    assert bad.calls == 2  # tried again after the cooldown


def test_pool_does_not_mask_other_errors():
    pool = KeyPoolProvider("x", [_Fake(ValueError("bad request")), _Fake()], ["a", "b"])
    with pytest.raises(ValueError):
        asyncio.run(pool.complete(_req()))


@pytest.fixture
def api(monkeypatch):
    import api.server as server
    from api.auth import create_access_token
    from api.keys_api import router

    monkeypatch.setitem(server.app_state, "router", Router({}))
    app = FastAPI()
    app.include_router(router)

    def h(scopes):
        return {"Authorization": "Bearer " + create_access_token("u", scopes)}

    return TestClient(app), h, server.app_state["router"]


def test_api_add_list_delete_applies_live(api):
    c, h, rt = api
    assert c.get("/v1/keys", headers=h(["completions:read"])).status_code == 403
    cat = c.get("/v1/keys/catalog", headers=h(["admin"])).json()["data"]
    assert {"openai", "deepseek", "qwen", "local", "custom"} <= {x["id"] for x in cat}
    r = c.post(
        "/v1/keys",
        headers=h(["admin"]),
        json={"provider": "openai", "api_key": "sk-aaaaaaaa1111"},
    )
    assert (
        r.status_code == 201 and r.json()["loaded"] and "sk-aaaaaaaa1111" not in r.text
    )
    assert "openai" in rt.providers and "openai" in rt.breakers
    r2 = c.post(
        "/v1/keys",
        headers=h(["admin"]),
        json={"provider": "openai", "api_key": "sk-bbbbbbbb2222", "label": "b"},
    )
    assert len(rt.providers["openai"].members) == 2
    rows = c.get("/v1/keys", headers=h(["admin"])).json()["data"]
    assert len(rows) == 2 and all("sk-" not in str(x) for x in rows)
    assert (
        c.delete(f"/v1/keys/{r2.json()['id']}", headers=h(["admin"])).status_code == 200
    )
    assert not hasattr(rt.providers["openai"], "members")
    assert (
        c.delete(f"/v1/keys/{r.json()['id']}", headers=h(["admin"])).status_code == 200
    )
    assert "openai" not in rt.providers and "openai" not in rt.breakers
    assert c.delete("/v1/keys/env:openai", headers=h(["admin"])).status_code == 400
    assert (
        c.post("/v1/keys", headers=h(["admin"]), json={"provider": "x"}).status_code
        == 400
    )


def test_server_starts_with_no_providers(monkeypatch, tmp_path):
    import api.server as server

    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    with TestClient(server.app) as c:
        assert c.get("/health").json()["providers"] == []


def test_cli_keys_add_reads_secret_from_stdin_not_argv(monkeypatch):
    import io
    import json

    import httpx

    from caips_cli.cli import main

    seen = {}

    def handler(req: httpx.Request):
        seen["body"] = json.loads(req.content)
        return httpx.Response(201, json={"id": "x"})

    monkeypatch.setattr("sys.stdin", io.StringIO("sk-fromstdin1234\n"))
    out = io.StringIO()
    rc = main(
        ["keys", "add", "qwen", "--label", "team"],
        transport=httpx.MockTransport(handler),
        out=out,
    )
    assert rc == 0
    assert seen["body"] == {
        "provider": "qwen",
        "api_key": "sk-fromstdin1234",
        "label": "team",
        "base_url": "",
        "model": "",
    }
