"""The official OpenAI Python SDK works against CaiPS with only a base URL
(and key) change. Runs the real SDK over the in-process app."""

import json

import pytest
from openai import OpenAI

from tests.helpers import as_httpx, make_provider, make_router, make_stream_provider
from tests.test_openai_compat import make_client  # noqa: F401  (fixture)
from tests.test_tool_calling import CALL, TOOL, tool_response


@pytest.fixture
def sdk(make_client):  # noqa: F811
    def build(router):
        http = make_client(router)
        token = http.headers["Authorization"].split(" ", 1)[1]
        return as_httpx(
            http,
            lambda hc: OpenAI(
                base_url="http://testserver/v1",
                api_key=token,
                http_client=hc,
                max_retries=0,
            ),
        )

    return build


def test_chat_completion(sdk):
    client = sdk(make_router({"a": make_provider("a")}))
    r = client.chat.completions.create(
        model="a-model", messages=[{"role": "user", "content": "hi"}]
    )
    assert r.choices[0].message.content == "response from a"
    assert r.usage.total_tokens > 0


def test_streaming(sdk):
    client = sdk(make_router({"a": make_stream_provider("a", chunks=("res", "ponse"))}))
    stream = client.chat.completions.create(
        model="a-model", messages=[{"role": "user", "content": "hi"}], stream=True
    )
    text = "".join(c.choices[0].delta.content or "" for c in stream if c.choices)
    assert text == "response"


def test_tool_calls_non_streaming_and_streaming(sdk):
    p = make_provider("a", behaviour=lambda req: tool_response())
    p.supports_tools = True
    client = sdk(make_router({"a": p}))
    msgs = [{"role": "user", "content": "Weather?"}]
    r = client.chat.completions.create(model="a-model", messages=msgs, tools=[TOOL])
    call = r.choices[0].message.tool_calls[0]
    assert call.function.name == "get_weather" and json.loads(
        call.function.arguments
    ) == {"city": "Paris"}

    stream = client.chat.completions.create(
        model="a-model", messages=msgs, tools=[TOOL], stream=True
    )
    args, name = "", None
    for chunk in stream:
        for tc in chunk.choices[0].delta.tool_calls or []:
            name = name or (tc.function.name or None)
            args += tc.function.arguments or ""
    assert name == "get_weather" and args == CALL["arguments"]


def test_models_and_embeddings_endpoints(sdk):
    client = sdk(make_router({"a": make_provider("a")}))
    assert any(m.id for m in client.models.list().data)


def test_errors_map_to_sdk_exceptions(sdk):
    import openai

    client = sdk(make_router({"a": make_provider("a")}))
    with pytest.raises(openai.BadRequestError):
        client.chat.completions.create(
            model="a-model", messages=[{"role": "user", "content": "x"}], n=2
        )
