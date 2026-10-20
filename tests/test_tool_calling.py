"""Normalised tool calling and structured output across the gateway."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from neural.model import get_registry
from providers.base import (
    CompletionRequest,
    CompletionResponse,
    Message,
    validate_conversation,
)
from tests.helpers import make_provider, make_router
from tests.test_openai_compat import BODY, make_client  # noqa: F401  (fixture)

TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Weather for a city",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
}
CALL = {"id": "call_1", "name": "get_weather", "arguments": '{"city": "Paris"}'}


def convo():
    return [
        Message(role="user", content="Weather in Paris?"),
        Message(role="assistant", content="", tool_calls=[CALL]),
        Message(role="tool", content="18C and sunny", tool_call_id="call_1"),
    ]


def tool_response():
    return CompletionResponse(
        content="", model="a-model", provider="a", input_tokens=5, output_tokens=2,
        cost_usd=0.001, latency_ms=1.0, metadata={"finish_reason": "tool_calls", "tool_calls": [CALL]},
    )  # fmt: skip


# ---------------------------------------------------------------- validation


def test_conversation_may_end_with_a_tool_result():
    validate_conversation(convo())


def test_tool_message_needs_an_id():
    msgs = [Message(role="user", content="x"), Message(role="tool", content="y")]
    with pytest.raises(ValueError, match="tool_call_id"):
        validate_conversation(msgs)


def test_cacheable_is_false_for_tool_traffic():
    assert CompletionRequest(messages=[Message(role="user", content="x")]).cacheable
    assert not CompletionRequest(
        messages=[Message(role="user", content="x")], tools=[TOOL]
    ).cacheable
    assert not CompletionRequest(messages=convo()).cacheable


def test_catalogue_advertises_function_calling():
    reg = get_registry()
    assert reg.get("gpt-4o").supports_function_calling
    assert not reg.get("deepseek-reasoner").supports_function_calling


# ------------------------------------------------------------------ endpoint


def test_tools_reach_the_provider_and_calls_come_back_in_openai_shape(
    make_client,
):  # noqa: F811
    p = make_provider("a", behaviour=lambda req: tool_response())
    p.supports_tools = True
    c = make_client(make_router({"a": p}))
    r = c.post(
        "/v1/chat/completions", json={**BODY, "tools": [TOOL], "tool_choice": "auto"}
    )
    assert r.status_code == 200
    choice = r.json()["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["content"] is None
    assert choice["message"]["tool_calls"] == [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'},
        }
    ]
    req = p.complete.call_args.args[0]
    assert req.tools == [TOOL] and req.tool_choice == "auto"


def test_tool_round_trip_messages_are_converted(make_client):  # noqa: F811
    p = make_provider("a")
    p.supports_tools = True
    c = make_client(make_router({"a": p}))
    messages = [
        {"role": "user", "content": "Weather in Paris?"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "arguments": '{"city":"Paris"}',
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "18C"},
    ]
    r = c.post("/v1/chat/completions", json={"messages": messages, "tools": [TOOL]})
    assert r.status_code == 200, r.text
    got = p.complete.call_args.args[0].messages
    assert got[1].tool_calls == [
        {"id": "call_1", "name": "get_weather", "arguments": '{"city":"Paris"}'}
    ]
    assert (got[2].role, got[2].tool_call_id, got[2].content) == (
        "tool",
        "call_1",
        "18C",
    )


@pytest.mark.parametrize(
    "extra,param",
    [
        ({"tools": [{"type": "function"}]}, "tools[0]"),
        ({"tools": [TOOL], "tool_choice": "sometimes"}, "tool_choice"),
        (
            {
                "tools": [TOOL],
                "tool_choice": {"type": "function", "function": {"name": "nope"}},
            },
            "tool_choice",
        ),
        ({"tool_choice": "auto"}, "tool_choice"),
        (
            {"response_format": {"type": "json_schema", "json_schema": {"name": "x"}}},
            "response_format",
        ),
    ],
)
def test_bad_tool_and_format_requests_are_400(make_client, extra, param):  # noqa: F811
    p = make_provider("a")
    r = make_client(make_router({"a": p})).post(
        "/v1/chat/completions", json={**BODY, **extra}
    )
    assert r.status_code == 400
    assert r.json()["error"]["param"] == param
    p.complete.assert_not_called()


def test_tool_message_without_id_is_400(make_client):  # noqa: F811
    p = make_provider("a")
    msgs = [{"role": "user", "content": "x"}, {"role": "tool", "content": "y"}]
    r = make_client(make_router({"a": p})).post(
        "/v1/chat/completions", json={"messages": msgs}
    )
    assert (
        r.status_code == 400
        and r.json()["error"]["param"] == "messages[1].tool_call_id"
    )


def test_provider_without_tool_support_is_not_used(make_client):  # noqa: F811
    p = make_provider("a")
    p.supports_tools = False
    r = make_client(make_router({"a": p})).post(
        "/v1/chat/completions", json={**BODY, "tools": [TOOL]}
    )
    assert r.status_code == 400
    p.complete.assert_not_called()


def test_response_format_is_forwarded(make_client):  # noqa: F811
    p = make_provider("a", behaviour=lambda req: _text_response("{}"))
    c = make_client(make_router({"a": p}))
    rf = {
        "type": "json_schema",
        "json_schema": {"name": "w", "schema": {"type": "object"}},
    }
    assert (
        c.post("/v1/chat/completions", json={**BODY, "response_format": rf}).status_code
        == 200
    )
    assert p.complete.call_args.args[0].response_format == rf
    c.post("/v1/chat/completions", json={**BODY, "response_format": {"type": "text"}})
    assert p.complete.call_args.args[0].response_format is None


# ------------------------------------------------------------------ OpenAI


def openai_message(content, calls=None):
    return SimpleNamespace(content=content, tool_calls=calls)


def openai_reply(message, finish="stop"):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish)],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=4),
    )


@pytest.mark.asyncio
async def test_openai_sends_tools_and_returns_calls():
    from providers.openai import OpenAIProvider

    with patch("providers.openai.AsyncOpenAI"):
        prov = OpenAIProvider(api_key="k")
    fn = SimpleNamespace(name="get_weather", arguments='{"city":"Paris"}')
    reply = openai_reply(
        openai_message(None, [SimpleNamespace(id="call_9", function=fn)]), "tool_calls"
    )
    prov.client.chat.completions.create = AsyncMock(return_value=reply)
    req = CompletionRequest(
        messages=convo()[:1], model="gpt-4o", tools=[TOOL], tool_choice="required"
    )
    out = await prov.complete(req)
    kwargs = prov.client.chat.completions.create.call_args.kwargs
    assert kwargs["tools"] == [TOOL] and kwargs["tool_choice"] == "required"
    assert out.content == ""
    assert out.metadata["finish_reason"] == "tool_calls"
    assert out.metadata["tool_calls"] == [
        {"id": "call_9", "name": "get_weather", "arguments": '{"city":"Paris"}'}
    ]


def test_openai_message_format_for_tool_turns():
    from providers.openai import OpenAIProvider

    with patch("providers.openai.AsyncOpenAI"):
        prov = OpenAIProvider(api_key="k")
    msgs = prov._build_messages(CompletionRequest(messages=convo()))
    assert msgs[1]["tool_calls"][0] == {
        "id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'},
    }  # fmt: skip
    assert msgs[1]["content"] is None
    assert msgs[2] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "18C and sunny",
    }


@pytest.mark.asyncio
async def test_deepseek_downgrades_json_schema_and_adds_a_prompt():
    from providers.deepseek import DeepSeekProvider

    with patch("providers.deepseek.AsyncOpenAI"):
        prov = DeepSeekProvider(api_key="k")
    prov.client.chat.completions.create = AsyncMock(
        return_value=openai_reply(openai_message('{"a":1}'))
    )
    schema = {"type": "object", "properties": {"a": {"type": "integer"}}}
    rf = {"type": "json_schema", "json_schema": {"name": "x", "schema": schema}}
    await prov.complete(CompletionRequest(messages=convo()[:1], response_format=rf))
    kwargs = prov.client.chat.completions.create.call_args.kwargs
    assert kwargs["response_format"] == {"type": "json_object"}
    assert (
        "JSON schema" in kwargs["messages"][0]["content"]
        and '"integer"' in kwargs["messages"][0]["content"]
    )


# --------------------------------------------------------------- Anthropic


@pytest.fixture
def anthropic_provider():
    with patch("providers.anthropic.sdk.AsyncAnthropic"):
        from providers.anthropic import AnthropicProvider

        return AnthropicProvider(api_key="k")


def test_anthropic_translates_tool_turns(anthropic_provider):
    msgs = convo() + [
        Message(role="assistant", content="", tool_calls=[{**CALL, "id": "call_2"}]),
        Message(role="tool", content="also 18C", tool_call_id="call_2"),
    ]
    out = anthropic_provider._build_messages(CompletionRequest(messages=msgs))
    assert out[1] == {
        "role": "assistant",
        "content": [
            {
                "type": "tool_use",
                "id": "call_1",
                "name": "get_weather",
                "input": {"city": "Paris"},
            }
        ],
    }
    assert out[2]["role"] == "user"
    assert out[2]["content"] == [
        {"type": "tool_result", "tool_use_id": "call_1", "content": "18C and sunny"}
    ]
    # results of different turns stay in separate messages
    assert out[3]["role"] == "assistant" and out[4]["role"] == "user"


def test_anthropic_merges_parallel_results(anthropic_provider):
    msgs = [
        Message(role="user", content="q"),
        Message(
            role="assistant", content="", tool_calls=[CALL, {**CALL, "id": "call_2"}]
        ),
        Message(role="tool", content="a", tool_call_id="call_1"),
        Message(role="tool", content="b", tool_call_id="call_2"),
    ]
    out = anthropic_provider._build_messages(CompletionRequest(messages=msgs))
    assert len(out) == 3
    assert [b["tool_use_id"] for b in out[2]["content"]] == ["call_1", "call_2"]


@pytest.mark.parametrize(
    "choice,expected",
    [
        ("auto", {"type": "auto"}),
        ("required", {"type": "any"}),
        (
            {"type": "function", "function": {"name": "get_weather"}},
            {"type": "tool", "name": "get_weather"},
        ),
        (None, None),
    ],
)
def test_anthropic_tool_kwargs(anthropic_provider, choice, expected):
    kw = anthropic_provider._tool_kwargs(
        CompletionRequest(messages=convo(), tools=[TOOL], tool_choice=choice)
    )
    assert kw["tools"] == [
        {
            "name": "get_weather",
            "description": "Weather for a city",
            "input_schema": TOOL["function"]["parameters"],
        }
    ]
    assert kw.get("tool_choice") == expected


def test_anthropic_tool_choice_none_sends_no_tools(anthropic_provider):
    req = CompletionRequest(messages=convo(), tools=[TOOL], tool_choice="none")
    assert anthropic_provider._tool_kwargs(req) == {}


@pytest.mark.asyncio
async def test_anthropic_returns_tool_calls_without_text(anthropic_provider):
    resp = MagicMock()
    resp.content = [
        SimpleNamespace(
            type="tool_use", id="tu_1", name="get_weather", input={"city": "Paris"}
        )
    ]
    resp.stop_reason = "tool_use"
    resp.usage.input_tokens, resp.usage.output_tokens = 12, 6
    anthropic_provider.client.messages.create = AsyncMock(return_value=resp)
    out = await anthropic_provider.complete(
        CompletionRequest(messages=convo()[:1], tools=[TOOL])
    )
    assert out.content == ""
    assert out.metadata["finish_reason"] == "tool_calls"
    assert out.metadata["tool_calls"] == [
        {"id": "tu_1", "name": "get_weather", "arguments": '{"city": "Paris"}'}
    ]


@pytest.mark.asyncio
async def test_anthropic_json_mode_uses_a_prompt_and_strips_fences(anthropic_provider):
    resp = MagicMock()
    resp.content = [SimpleNamespace(type="text", text='```json\n{"a": 1}\n```')]
    resp.stop_reason = "end_turn"
    resp.usage.input_tokens, resp.usage.output_tokens = 5, 5
    anthropic_provider.client.messages.create = AsyncMock(return_value=resp)
    req = CompletionRequest(
        messages=convo()[:1], response_format={"type": "json_object"}
    )
    out = await anthropic_provider.complete(req)
    assert json.loads(out.content) == {"a": 1}
    assert (
        "valid JSON"
        in anthropic_provider.client.messages.create.call_args.kwargs["system"]
    )


# ------------------------------------------------------------------ Gemini


@pytest.fixture
def gemini_provider():
    from providers.gemini import GeminiProvider

    with patch("providers.gemini.genai.Client"):
        return GeminiProvider(api_key="k")


def test_gemini_translates_tool_turns(gemini_provider):
    history, last = gemini_provider._build_messages(CompletionRequest(messages=convo()))
    assert history[1] == {
        "role": "model",
        "parts": [
            {"function_call": {"name": "get_weather", "args": {"city": "Paris"}}}
        ],
    }
    assert last == [
        {
            "function_response": {
                "name": "get_weather",
                "response": {"result": "18C and sunny"},
            }
        }
    ]


def test_gemini_plain_conversation_still_sends_a_string(gemini_provider):
    history, last = gemini_provider._build_messages(
        CompletionRequest(messages=[Message(role="user", content="hi")])
    )
    assert history == [] and last == "hi"


def test_gemini_config_carries_tools_and_json_mode(gemini_provider):
    req = CompletionRequest(
        messages=convo()[:1],
        tools=[TOOL],
        tool_choice="required",
        response_format={"type": "json_object"},
    )
    cfg = gemini_provider._build_config(req)
    decl = cfg.tools[0].function_declarations[0]
    assert decl.name == "get_weather"
    assert cfg.tool_config.function_calling_config.mode == "ANY"
    assert cfg.response_mime_type == "application/json"


@pytest.mark.asyncio
async def test_gemini_extracts_function_calls(gemini_provider):
    fc = SimpleNamespace(name="get_weather", args={"city": "Paris"})
    reply = MagicMock()
    reply.candidates = [
        SimpleNamespace(
            content=SimpleNamespace(
                parts=[SimpleNamespace(function_call=fc, text=None)]
            ),
            finish_reason="STOP",
        )
    ]
    (
        reply.usage_metadata.prompt_token_count,
        reply.usage_metadata.candidates_token_count,
    ) = (7, 3)
    chat = MagicMock()
    chat.send_message = AsyncMock(return_value=reply)
    gemini_provider.client.aio.chats.create = MagicMock(return_value=chat)
    out = await gemini_provider.complete(
        CompletionRequest(messages=convo()[:1], tools=[TOOL])
    )
    assert out.content == ""
    assert out.metadata["finish_reason"] == "tool_calls"
    assert out.metadata["tool_calls"] == [
        {"id": "call_0", "name": "get_weather", "arguments": '{"city": "Paris"}'}
    ]


# ------------------------------------------- buffered streaming + schema check

SCHEMA_RF = {
    "type": "json_schema",
    "json_schema": {
        "name": "w",
        "schema": {
            "type": "object",
            "properties": {"n": {"type": "integer"}},
            "required": ["n"],
        },
    },
}


def _text_response(text):
    return CompletionResponse(
        content=text, model="a-model", provider="a", input_tokens=5, output_tokens=2,
        cost_usd=0.001, latency_ms=1.0, metadata={},
    )  # fmt: skip


def test_streamed_tool_calls_arrive_as_sse_deltas(make_client):  # noqa: F811
    p = make_provider("a", behaviour=lambda req: tool_response())
    p.supports_tools = True
    c = make_client(make_router({"a": p}))
    r = c.post("/v1/chat/completions", json={**BODY, "tools": [TOOL], "stream": True})
    assert r.status_code == 200
    events = [
        json.loads(e[6:]) for e in r.text.split("\n\n") if e.startswith("data: {")
    ]
    calls = [
        c_ for e in events for c_ in e["choices"][0]["delta"].get("tool_calls", [])
    ]
    assert calls[0]["id"] == "call_1" and calls[0]["function"]["name"] == "get_weather"
    assert (
        "".join(c_["function"].get("arguments", "") for c_ in calls)
        == CALL["arguments"]
    )
    assert events[-1]["choices"][0]["finish_reason"] == "tool_calls"
    assert r.text.rstrip().endswith("data: [DONE]")


def test_schema_valid_output_passes(make_client):  # noqa: F811
    p = make_provider("a", behaviour=lambda req: _text_response('{"n": 1}'))
    c = make_client(make_router({"a": p}))
    r = c.post("/v1/chat/completions", json={**BODY, "response_format": SCHEMA_RF})
    assert r.status_code == 200 and p.complete.call_count == 1


def test_schema_violation_is_retried_once_then_passes(make_client):  # noqa: F811
    replies = iter(['{"n": "x"}', '{"n": 2}'])
    p = make_provider("a", behaviour=lambda req: _text_response(next(replies)))
    c = make_client(make_router({"a": p}))
    r = c.post("/v1/chat/completions", json={**BODY, "response_format": SCHEMA_RF})
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == '{"n": 2}'
    assert p.complete.call_count == 2


def test_schema_violation_twice_is_502(make_client):  # noqa: F811
    p = make_provider("a", behaviour=lambda req: _text_response("not json"))
    c = make_client(make_router({"a": p}))
    r = c.post("/v1/chat/completions", json={**BODY, "response_format": SCHEMA_RF})
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "schema_validation_failed"
