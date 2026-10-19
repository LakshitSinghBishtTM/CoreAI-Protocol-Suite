"""Image inputs and the Anthropic-compatible /v1/messages endpoint."""

import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from providers.base import CompletionRequest, CompletionResponse, Message
from tests.helpers import make_provider, make_router, make_stream_provider
from tests.test_openai_compat import make_client  # noqa: F401  (fixture)

PNG = base64.b64encode(b"\x89PNG fake").decode()
DATA_URL = f"data:image/png;base64,{PNG}"
IMG = {"media_type": "image/png", "data": PNG}


def user_with_image():
    return Message(role="user", content="what is this?", images=[IMG])


# ------------------------------------------------------------------ providers


def test_openai_family_sends_image_url_parts():
    from providers.base import openai_messages

    out = openai_messages(CompletionRequest(messages=[user_with_image()]))
    assert out[0]["content"] == [
        {"type": "text", "text": "what is this?"},
        {"type": "image_url", "image_url": {"url": DATA_URL}},
    ]
    link = Message(role="user", content="", images=[{"url": "https://x.test/a.png"}])
    assert openai_messages(CompletionRequest(messages=[link]))[0]["content"] == [
        {"type": "image_url", "image_url": {"url": "https://x.test/a.png"}}
    ]


def test_anthropic_image_blocks():
    with patch("providers.anthropic.sdk.AsyncAnthropic"):
        from providers.anthropic import AnthropicProvider

        prov = AnthropicProvider(api_key="k")
    msgs = [Message(role="user", content="q", images=[IMG, {"url": "https://x.test/a.png"}])]
    out = prov._build_messages(CompletionRequest(messages=msgs))
    assert out[0]["content"] == [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": PNG}},
        {"type": "image", "source": {"type": "url", "url": "https://x.test/a.png"}},
        {"type": "text", "text": "q"},
    ]


def test_gemini_inline_data_and_link_refusal():
    from providers.gemini import GeminiProvider

    with patch("providers.gemini.genai.Client"):
        prov = GeminiProvider(api_key="k")
    history, last = prov._build_messages(CompletionRequest(messages=[user_with_image()]))
    assert history == []
    assert last == [
        {"inline_data": {"mime_type": "image/png", "data": b"\x89PNG fake"}},
        {"text": "what is this?"},
    ]
    link = Message(role="user", content="x", images=[{"url": "https://x.test/a.png"}])
    with pytest.raises(ValueError, match="inline data"):
        prov._build_messages(CompletionRequest(messages=[link]))


def test_images_are_never_cached():
    assert not CompletionRequest(messages=[user_with_image()]).cacheable


# ------------------------------------------------------------ chat completions


def vision_body(url=DATA_URL):
    part = {"type": "image_url", "image_url": {"url": url}}
    return {"messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}, part]}]}


def test_chat_accepts_image_parts(make_client):  # noqa: F811
    p = make_provider("a")
    p.supports_vision = True
    r = make_client(make_router({"a": p})).post("/v1/chat/completions", json=vision_body())
    assert r.status_code == 200, r.text
    msg = p.complete.call_args.args[0].messages[0]
    assert msg.content == "hi" and msg.images == [IMG]


@pytest.mark.parametrize("url", ["ftp://x/a.png", "data:text/plain;base64,aGk=", "data:image/png,notbase64", ""])
def test_chat_rejects_bad_image_urls(make_client, url):  # noqa: F811
    p = make_provider("a")
    r = make_client(make_router({"a": p})).post("/v1/chat/completions", json=vision_body(url))
    assert r.status_code == 400
    p.complete.assert_not_called()


def test_chat_refuses_images_when_provider_cannot_see(make_client):  # noqa: F811
    p = make_provider("a")
    p.supports_vision = False
    r = make_client(make_router({"a": p})).post("/v1/chat/completions", json=vision_body())
    assert r.status_code == 400
    p.complete.assert_not_called()


def test_chat_refuses_images_for_text_only_model(make_client):  # noqa: F811
    p = make_provider("deepseek")
    p.supports_vision = True
    body = {**vision_body(), "model": "deepseek-chat"}
    r = make_client(make_router({"deepseek": p})).post("/v1/chat/completions", json=body)
    assert r.status_code == 400
    p.complete.assert_not_called()


# --------------------------------------------------------------- /v1/messages

TOOL = {"name": "get_weather", "description": "Weather", "input_schema": {"type": "object", "properties": {}}}
MSG = {"model": "a-model", "max_tokens": 50, "messages": [{"role": "user", "content": "hi"}]}


def test_messages_response_shape(make_client):  # noqa: F811
    c = make_client(make_router({"a": make_provider("a")}))
    r = c.post("/v1/messages", json=MSG)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["type"] == "message" and j["role"] == "assistant" and j["id"].startswith("msg_")
    assert j["content"] == [{"type": "text", "text": "response from a"}]
    assert j["stop_reason"] == "end_turn"
    assert j["usage"] == {"input_tokens": 20, "output_tokens": 10}


def test_messages_maps_system_images_and_text(make_client):  # noqa: F811
    p = make_provider("a")
    p.supports_vision = True
    body = {
        **MSG,
        "system": [{"type": "text", "text": "be brief"}, {"type": "text", "text": "no lists"}],
        "temperature": 0.2,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": PNG}},
                    {"type": "text", "text": "what?"},
                ],
            }
        ],
    }
    r = make_client(make_router({"a": p})).post("/v1/messages", json=body)
    assert r.status_code == 200, r.text
    req = p.complete.call_args.args[0]
    assert req.system_prompt == "be briefno lists" and req.temperature == 0.2 and req.max_tokens == 50
    assert req.messages[0].content == "what?" and req.messages[0].images == [IMG]


def test_messages_tool_round_trip(make_client):  # noqa: F811
    call = {"id": "tu_1", "name": "get_weather", "arguments": '{"city": "Paris"}'}
    reply = CompletionResponse(
        content="Let me check.", model="a-model", provider="a", input_tokens=5, output_tokens=2,
        cost_usd=0.0, latency_ms=1.0, metadata={"finish_reason": "tool_calls", "tool_calls": [call]},
    )  # fmt: skip
    p = make_provider("a", behaviour=lambda req: reply)
    p.supports_tools = True
    c = make_client(make_router({"a": p}))
    j = c.post("/v1/messages", json={**MSG, "tools": [TOOL], "tool_choice": {"type": "any"}}).json()
    assert j["stop_reason"] == "tool_use"
    assert j["content"] == [
        {"type": "text", "text": "Let me check."},
        {"type": "tool_use", "id": "tu_1", "name": "get_weather", "input": {"city": "Paris"}},
    ]
    req = p.complete.call_args.args[0]
    assert req.tool_choice == "required"
    assert req.tools[0]["function"] == {"name": "get_weather", "description": "Weather", "parameters": TOOL["input_schema"]}

    follow = {
        **MSG,
        "tools": [TOOL],
        "messages": [
            {"role": "user", "content": "weather?"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "tu_1", "name": "get_weather", "input": {"city": "Paris"}}]},
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "tu_1", "content": [{"type": "text", "text": "18C"}]},
                    {"type": "text", "text": "thanks"},
                ],
            },
        ],
    }
    assert c.post("/v1/messages", json=follow).status_code == 200
    got = p.complete.call_args.args[0].messages
    assert [m.role for m in got] == ["user", "assistant", "tool", "user"]
    assert got[1].tool_calls == [{"id": "tu_1", "name": "get_weather", "arguments": '{"city": "Paris"}'}]
    assert (got[2].tool_call_id, got[2].content) == ("tu_1", "18C")


@pytest.mark.parametrize(
    "extra",
    [
        {"stop_sequences": ["x"]},
        {"tools": [TOOL], "stream": True},
        {"tool_choice": {"type": "auto"}},
        {"tools": [TOOL], "tool_choice": {"type": "tool", "name": "nope"}},
        {"tools": [{"name": "x", "type": "web_search_20250305"}]},
        {"messages": [{"role": "user", "content": [{"type": "document", "source": {}}]}]},
        {"messages": [{"role": "system", "content": "x"}]},
    ],
)
def test_messages_refuses_unsupported_requests(make_client, extra):  # noqa: F811
    p = make_provider("a")
    r = make_client(make_router({"a": p})).post("/v1/messages", json={**MSG, **extra})
    assert r.status_code == 400
    assert r.json()["type"] == "error" and r.json()["error"]["type"] == "invalid_request_error"
    p.complete.assert_not_called()


def test_messages_requires_max_tokens(make_client):  # noqa: F811
    body = {k: v for k, v in MSG.items() if k != "max_tokens"}
    r = make_client(make_router({"a": make_provider("a")})).post("/v1/messages", json=body)
    assert r.status_code == 422


def sse_events(text):
    out = []
    for chunk in text.split("\n\n"):
        lines = dict(line.split(": ", 1) for line in chunk.splitlines() if ": " in line)
        if lines:
            out.append((lines["event"], json.loads(lines["data"])))
    return out


def test_messages_streaming_event_sequence(make_client):  # noqa: F811
    c = make_client(make_router({"a": make_stream_provider("a", chunks=("Hel", "lo"))}))
    r = c.post("/v1/messages", json={**MSG, "stream": True})
    assert r.status_code == 200
    events = sse_events(r.text)
    assert [e for e, _ in events] == [
        "message_start", "content_block_start", "content_block_delta", "content_block_delta",
        "content_block_stop", "message_delta", "message_stop",
    ]  # fmt: skip
    assert "".join(d["delta"]["text"] for e, d in events if e == "content_block_delta") == "Hello"
    assert events[-2][1]["delta"]["stop_reason"] == "end_turn"


def test_messages_stream_failure_after_output_ends_with_error_and_no_stop(make_client):  # noqa: F811
    p = make_stream_provider("a", chunks=("a", "b", "c"), fail_after=(1, RuntimeError("boom")))
    r = make_client(make_router({"a": p})).post("/v1/messages", json={**MSG, "stream": True})
    names = [e for e, _ in sse_events(r.text)]
    assert names[-1] == "error" and "message_stop" not in names and "message_delta" not in names


def test_messages_error_before_output_is_an_http_error(make_client):  # noqa: F811
    p = make_stream_provider("a", open_error=RuntimeError("down"))
    r = make_client(make_router({"a": p})).post("/v1/messages", json={**MSG, "stream": True})
    assert r.status_code >= 400 and r.json()["type"] == "error"


def test_messages_needs_the_write_scope(make_client):  # noqa: F811
    c = make_client(make_router({"a": make_provider("a")}), scopes=("completions:read",))
    assert c.post("/v1/messages", json=MSG).status_code == 403


def test_official_anthropic_sdk_works_with_only_a_base_url_change(make_client):  # noqa: F811
    anthropic = pytest.importorskip("anthropic")
    c = make_client(make_router({"a": make_stream_provider("a", chunks=("Hel", "lo"))}))
    token = c.headers["Authorization"].split()[1]
    sdk = anthropic.Anthropic(base_url="http://testserver", auth_token=token, http_client=c, max_retries=0)
    msg = sdk.messages.create(model="a-model", max_tokens=20, messages=MSG["messages"])
    assert msg.content[0].text and msg.stop_reason == "end_turn"
    with sdk.messages.stream(model="a-model", max_tokens=20, messages=MSG["messages"]) as stream:
        assert "".join(stream.text_stream) == "Hello"
        final = stream.get_final_message()
    assert final.stop_reason == "end_turn"
