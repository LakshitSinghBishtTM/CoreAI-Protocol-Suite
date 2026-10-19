"""OpenAI-compatible endpoints, including the official OpenAI SDK."""

import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.helpers import StatusError, make_provider, make_router, make_stream_provider

BODY = {"model": "a-model", "messages": [{"role": "user", "content": "hi"}]}


@pytest.fixture
def make_client(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "openai-compat-test-secret")
    import api.server as server
    from api.auth import create_access_token
    from api.openai_compat import router as compat
    from api.routes import router as api_router

    started = []

    def build(router, scopes=("completions:read", "completions:write")):
        app = FastAPI()
        app.include_router(api_router)
        app.include_router(compat)
        orch = MagicMock()
        orch.stats.return_value = {}
        for p in (
            patch.object(server, "get_router", return_value=router),
            patch.object(server, "get_orchestrator", return_value=orch),
        ):
            p.start()
            started.append(p)
        c = TestClient(app, base_url="http://testserver")
        c.headers["Authorization"] = "Bearer " + create_access_token("u", list(scopes))
        return c

    yield build
    for p in started:
        p.stop()


def sse(text):
    events = [e[6:] for e in text.split("\n\n") if e.startswith("data: ")]
    return [e if e == "[DONE]" else json.loads(e) for e in events]


def test_chat_completion_shape(make_client):
    c = make_client(make_router({"a": make_provider("a", cost=0.5)}))
    r = c.post("/v1/chat/completions", json=BODY)
    assert r.status_code == 200
    j = r.json()
    assert j["object"] == "chat.completion" and j["id"].startswith("chatcmpl-")
    assert j["choices"][0]["message"] == {
        "role": "assistant", "content": "response from a"
    }  # fmt: skip
    assert j["choices"][0]["finish_reason"] == "stop"
    assert j["usage"] == {
        "prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30
    }  # fmt: skip
    assert r.headers["x-caips-provider"] == "a"


def test_system_and_text_parts_are_mapped(make_client):
    p = make_provider("a")
    c = make_client(make_router({"a": p}))
    c.post(
        "/v1/chat/completions",
        json={
            "messages": [
                {"role": "system", "content": "be brief"},
                {"role": "developer", "content": "no lists"},
                {"role": "user", "content": [{"type": "text", "text": "hi"}]},
            ],
            "max_completion_tokens": 77,
        },
    )
    req = p.complete.call_args.args[0]
    assert req.system_prompt == "be brief\n\nno lists"
    assert [(m.role, m.content) for m in req.messages] == [("user", "hi")]
    assert req.max_tokens == 77


@pytest.mark.parametrize(
    "extra,param",
    [
        ({"stop": ["x"]}, "stop"),
        ({"n": 2}, "n"),
        ({"response_format": {"type": "xml"}}, "response_format"),
        ({"tools": [{"type": "function", "function": {"name": "f"}}], "stream": True}, "tools"),
    ],
)
def test_unsupported_parameters_are_refused_not_dropped(make_client, extra, param):
    p = make_provider("a")
    r = make_client(make_router({"a": p})).post(
        "/v1/chat/completions", json={**BODY, **extra}
    )
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "unsupported_parameter" and err["param"] == param
    p.complete.assert_not_called()


def test_image_part_refused_and_harmless_params_ignored(make_client):
    c = make_client(make_router({"a": make_provider("a")}))
    img = {"role": "user", "content": [{"type": "image_url", "image_url": {}}]}
    r = c.post("/v1/chat/completions", json={"messages": [img]})
    assert r.status_code == 400 and "image_url" in r.json()["error"]["message"]
    ok = c.post("/v1/chat/completions", json={**BODY, "top_p": 0.5, "user": "x"})
    assert ok.status_code == 200


def test_errors_use_openai_envelope(make_client):
    router = make_router(
        {"a": make_provider("a", behaviour=StatusError(529))}, max_retry_attempts=1
    )
    r = make_client(router).post("/v1/chat/completions", json=BODY)
    assert r.status_code == 503
    assert set(r.json()["error"]) == {"message", "type", "param", "code"}


def test_budget_429_keeps_retry_after(make_client):
    import api.server as server
    from coreai.budgets import BudgetLimits, BudgetManager

    c = make_client(make_router({"a": make_provider("a")}))
    mgr = BudgetManager(BudgetLimits(max_requests=0))
    with patch.object(server, "get_budgets", return_value=mgr):
        r = c.post("/v1/chat/completions", json=BODY)
    assert r.status_code == 429
    assert r.json()["error"]["type"] == "rate_limit_error"
    assert int(r.headers["retry-after"]) >= 1


def test_stream_events_end_with_stop_usage_and_done(make_client):
    c = make_client(make_router({"a": make_stream_provider("a", chunks=("Hel", "lo"))}))
    r = c.post(
        "/v1/chat/completions",
        json={**BODY, "stream": True, "stream_options": {"include_usage": True}},
    )
    assert r.headers["content-type"].startswith("text/event-stream")
    ev = sse(r.text)
    assert ev[-1] == "[DONE]"
    chunks = [e for e in ev[:-1]]
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    text = "".join(
        e["choices"][0]["delta"].get("content", "") for e in chunks if e["choices"]
    )
    assert text == "Hello"
    finals = [e for e in chunks if e["choices"] and e["choices"][0]["finish_reason"]]
    assert [f["choices"][0]["finish_reason"] for f in finals] == ["stop"]
    assert chunks[-1]["choices"] == [] and "total_tokens" in chunks[-1]["usage"]
    assert len({e["id"] for e in chunks}) == 1


def test_stream_that_cannot_open_is_http_error(make_client):
    router = make_router(
        {"a": make_stream_provider("a", open_error=StatusError(529))},
        max_retry_attempts=1,
    )
    r = make_client(router).post("/v1/chat/completions", json={**BODY, "stream": True})
    assert r.status_code == 503 and "error" in r.json()


def test_mid_stream_failure_sends_error_event_and_no_finish_reason(make_client):
    p = make_stream_provider(
        "a", chunks=("x", "y", "z"), fail_after=(1, StatusError(500))
    )
    r = make_client(make_router({"a": p}, max_retry_attempts=1)).post(
        "/v1/chat/completions", json={**BODY, "stream": True}
    )
    assert r.status_code == 200
    ev = sse(r.text)
    assert ev[-1] == "[DONE]"
    assert any(isinstance(e, dict) and "error" in e for e in ev)
    assert not any(
        isinstance(e, dict) and e.get("choices") and e["choices"][0]["finish_reason"]
        for e in ev
    )


def test_models_list_is_openai_shaped(make_client):
    c = make_client(make_router({"a": make_provider("a")}))
    j = c.get("/v1/models").json()
    assert j["object"] == "list"
    assert {"id": "a-model", "object": "model", "created": 0, "owned_by": "a"} in j[
        "data"
    ]
    assert c.get("/v1/models/details").status_code == 200  # the richer registry


def test_scope_is_enforced(make_client):
    c = make_client(
        make_router({"a": make_provider("a")}), scopes=("completions:read",)
    )
    assert c.post("/v1/chat/completions", json=BODY).status_code == 403


def test_official_openai_sdk_works_with_only_a_base_url_change(make_client):
    openai = pytest.importorskip("openai")
    router = make_router({"a": make_stream_provider("a", chunks=("Hel", "lo"))})
    c = make_client(router)
    token = c.headers["Authorization"].split()[1]
    sdk = openai.OpenAI(
        base_url="http://testserver/v1", api_key=token, http_client=c, max_retries=0
    )
    comp = sdk.chat.completions.create(model="a-model", messages=BODY["messages"])
    assert comp.choices[0].message.content
    parts = sdk.chat.completions.create(
        model="a-model", messages=BODY["messages"], stream=True
    )
    assert (
        "".join(p.choices[0].delta.content or "" for p in parts if p.choices) == "Hello"
    )
    assert [m.id for m in sdk.models.list().data] == ["a-model"]
    with pytest.raises(openai.BadRequestError):
        sdk.chat.completions.create(model="a-model", messages=BODY["messages"], n=2)
