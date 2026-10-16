"""
Contract tests: the real provider classes and real SDKs, over mock HTTP
transports (no network). These catch SDK drift that mocks of our own code
cannot, such as a parameter the SDK no longer accepts, and they check that
real SDK exceptions are classified correctly for retry and failover.
"""

import json

import httpx
import pytest
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI

from coreai.errors import ErrorKind, classify
from providers.anthropic import AnthropicProvider
from providers.base import CompletionRequest, Message
from providers.openai import OpenAIProvider
from tests.helpers import make_router, request

try:  # Anthropic SDK 1.x ships its own HTTP layer; older versions use httpx
    import httpx2
except ImportError:  # pragma: no cover
    httpx2 = httpx

ANTHROPIC_OK = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "model": "claude-test",
    "content": [{"type": "text", "text": "hello from claude"}],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {"input_tokens": 12, "output_tokens": 4},
}

OPENAI_OK = {
    "id": "c1",
    "object": "chat.completion",
    "created": 1,
    "model": "gpt-test",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "hello from gpt"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12},
}


def anthropic_provider(handler):
    p = AnthropicProvider("test-key")
    p.client = AsyncAnthropic(
        api_key="k",
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )
    return p


def openai_provider(handler):
    p = OpenAIProvider("test-key")
    p.client = AsyncOpenAI(
        api_key="k",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return p


def status_handler(mod, status, headers=None):
    def handler(req):
        return mod.Response(
            status,
            headers=headers or {},
            json={"error": {"type": "e", "message": f"status {status}"}},
        )

    return handler


def req(**kw):
    return CompletionRequest(
        messages=[Message(role="user", content="hi")], max_tokens=50, **kw
    )


# ------------------------------------------------------------------ Anthropic


@pytest.mark.asyncio
async def test_anthropic_complete_works_with_the_installed_sdk():
    """Regression: the provider passed `temperature`, which the current SDK
    rejects with a TypeError, so every Anthropic call failed."""
    seen = {}

    def handler(r):
        seen["body"] = json.loads(r.content)
        return httpx2.Response(200, json=ANTHROPIC_OK)

    resp = await anthropic_provider(handler).complete(
        req(temperature=0.2, system_prompt="be brief")
    )
    assert resp.content == "hello from claude"
    assert (resp.input_tokens, resp.output_tokens) == (12, 4)
    assert "temperature" not in seen["body"]
    assert seen["body"]["system"] == "be brief"
    assert seen["body"]["max_tokens"] == 50


@pytest.mark.asyncio
async def test_anthropic_stream_works_with_the_installed_sdk():
    events = [
        (
            "message_start",
            {"type": "message_start", "message": {**ANTHROPIC_OK, "content": []}},
        ),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "Hel"},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "lo"},
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 2},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]
    sse = "".join(f"event: {n}\ndata: {json.dumps(d)}\n\n" for n, d in events)
    seen = {}

    def handler(r):
        seen["body"] = json.loads(r.content)
        return httpx2.Response(
            200, headers={"content-type": "text/event-stream"}, content=sse.encode()
        )

    chunks = [c async for c in anthropic_provider(handler).stream(req(temperature=0.9))]
    assert "".join(chunks) == "Hello"
    assert "temperature" not in seen["body"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,headers,kind,retry_after",
    [
        (529, {"retry-after": "3"}, ErrorKind.OVERLOADED, 3.0),
        (429, {"retry-after": "7"}, ErrorKind.RATE_LIMIT, 7.0),
        (500, None, ErrorKind.SERVER_ERROR, None),
        (503, None, ErrorKind.OVERLOADED, None),
        (401, None, ErrorKind.AUTH, None),
        (400, None, ErrorKind.BAD_REQUEST, None),
    ],
)
async def test_real_anthropic_errors_are_classified(status, headers, kind, retry_after):
    provider = anthropic_provider(status_handler(httpx2, status, headers))
    with pytest.raises(Exception) as info:
        await provider.complete(req())
    result = classify(info.value)
    assert result.kind is kind
    assert result.retry_after == retry_after


@pytest.mark.asyncio
async def test_anthropic_timeout_and_connection_errors_are_classified():
    def timeout(r):
        raise httpx2.ReadTimeout("slow", request=r)

    def refused(r):
        raise httpx2.ConnectError("refused", request=r)

    for handler, kind in (
        (timeout, ErrorKind.TIMEOUT),
        (refused, ErrorKind.CONNECTION),
    ):
        with pytest.raises(Exception) as info:
            await anthropic_provider(handler).complete(req())
        assert classify(info.value).kind is kind


# ------------------------------------------------------------------ OpenAI


@pytest.mark.asyncio
async def test_openai_complete_works_with_the_installed_sdk():
    resp = await openai_provider(
        lambda r: httpx.Response(200, json=OPENAI_OK)
    ).complete(req())
    assert resp.content == "hello from gpt"
    assert (resp.input_tokens, resp.output_tokens) == (9, 3)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,headers,kind,retry_after",
    [
        (529, {"retry-after": "3"}, ErrorKind.OVERLOADED, 3.0),
        (429, {"retry-after-ms": "2500"}, ErrorKind.RATE_LIMIT, 2.5),
        (500, None, ErrorKind.SERVER_ERROR, None),
        (401, None, ErrorKind.AUTH, None),
        (400, None, ErrorKind.BAD_REQUEST, None),
    ],
)
async def test_real_openai_errors_are_classified(status, headers, kind, retry_after):
    provider = openai_provider(status_handler(httpx, status, headers))
    with pytest.raises(Exception) as info:
        await provider.complete(req())
    result = classify(info.value)
    assert result.kind is kind
    assert result.retry_after == retry_after


# ------------------------------------------------------------------ end to end


@pytest.mark.asyncio
async def test_router_fails_over_from_a_real_anthropic_529_to_openai():
    """The scenario from the roadmap, with real SDK exceptions end to end."""
    claude = anthropic_provider(status_handler(httpx2, 529, {"retry-after": "0"}))
    gpt = openai_provider(lambda r: httpx.Response(200, json=OPENAI_OK))
    router = make_router({"anthropic": claude, "openai": gpt}, max_retry_attempts=1)

    resp = await router.route(request())

    assert resp.content == "hello from gpt"
    assert resp.metadata["failed_over_from"] == ["anthropic"]
    stats = router.stats()["provider_stats"]["anthropic"]
    assert stats["error_kinds"] == {"overloaded": 1}


@pytest.mark.asyncio
async def test_router_retries_a_real_529_then_succeeds_on_the_same_provider():
    calls = {"n": 0}

    def handler(r):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx2.Response(
                529,
                headers={"retry-after": "0"},
                json={"error": {"type": "overloaded_error", "message": "Overloaded"}},
            )
        return httpx2.Response(200, json=ANTHROPIC_OK)

    router = make_router(
        {"anthropic": anthropic_provider(handler)},
        enable_retry=True,
        max_retry_attempts=3,
    )
    resp = await router.route(request())
    assert resp.content == "hello from claude" and calls["n"] == 2


# ------------------------------------------------------------------ Gemini


@pytest.mark.parametrize(
    "cls_name,code,status,kind",
    [
        ("ClientError", 429, "RESOURCE_EXHAUSTED", ErrorKind.RATE_LIMIT),
        ("ServerError", 503, "UNAVAILABLE", ErrorKind.OVERLOADED),
        ("ServerError", 500, "INTERNAL", ErrorKind.SERVER_ERROR),
        ("ClientError", 400, "INVALID_ARGUMENT", ErrorKind.BAD_REQUEST),
        ("ClientError", 403, "PERMISSION_DENIED", ErrorKind.AUTH),
    ],
)
def test_real_gemini_errors_are_classified(cls_name, code, status, kind):
    from google.genai import errors

    exc = getattr(errors, cls_name)(
        code, {"error": {"code": code, "message": "m", "status": status}}
    )
    assert classify(exc).kind is kind
