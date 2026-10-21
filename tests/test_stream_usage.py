"""Streamed responses are metered: tokens, cost, budgets and metrics."""

from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from coreai.budgets import BudgetLimits, BudgetManager
from coreai.metrics import render_prometheus
from providers import CompletionRequest, Message, StreamUsage
from tests.helpers import (
    FakeClock,
    StatusError,
    collect,
    make_provider,
    make_router,
    make_stream_provider,
    request,
)


def reporting_provider(name="p", chunks=("ab", "cd"), usage=(11, 7), cost=0.25, **kw):
    """A stream provider that reports token usage like a real SDK would."""
    p = make_stream_provider(name, chunks=chunks, **kw)
    inner = p.stream.side_effect

    async def gen(req):
        async for c in inner(req):
            yield c
        if req.stream_usage is not None and usage:
            req.stream_usage.report(usage[0], usage[1], f"{name}-real")

    p.stream = MagicMock(side_effect=gen)
    p._bill = MagicMock(return_value=cost)
    return p


# ------------------------------ StreamUsage ------------------------------


def test_report_ignores_junk_and_mocks():
    u = StreamUsage()
    u.report(MagicMock(), None)
    u.report(True, -3)
    assert not u.reported and u.input_tokens == 0
    u.report(5, None, "m")
    assert u.reported and u.input_tokens == 5 and u.output_tokens == 0
    u.report(None, 9)
    assert (u.input_tokens, u.output_tokens, u.model) == (5, 9, "m")


# ------------------------------ router settling ------------------------------


async def test_reported_usage_is_priced_and_counted():
    p = reporting_provider()
    router = make_router({"p": p})
    req = request()
    assert await collect(router.stream(req)) == ["ab", "cd"]

    u = req.stream_usage
    assert (u.input_tokens, u.output_tokens, u.model) == (11, 7, "p-real")
    assert u.reported and not u.estimated and u.completed and u.cost_usd == 0.25
    p._bill.assert_called_once_with(11, 7, "p-real")
    stats = router.stats()
    assert stats["provider_stats"]["p"]["total_cost"] == pytest.approx(0.25)
    row = stats["model_stats"][0]
    assert (row["input_tokens"], row["output_tokens"]) == (11, 7)
    assert stats["streams_estimated"] == {}


async def test_unreported_usage_is_estimated_and_flagged():
    p = make_stream_provider("p", chunks=("a" * 20, "b" * 20))
    p._bill = MagicMock(return_value=0.1)
    router = make_router({"p": p})
    req = request("x" * 80)
    await collect(router.stream(req))

    u = req.stream_usage
    assert u.estimated and not u.reported
    assert u.output_tokens == 10  # 40 chars / 4
    assert u.input_tokens == 20  # 80 chars / 4
    assert router.stats()["streams_estimated"] == {"p": 1}
    text = render_prometheus(router.stats())
    assert 'caips_stream_usage_estimated_total{provider="p"} 1' in text


async def test_stream_that_fails_midway_still_bills_what_was_delivered():
    p = make_stream_provider(
        "p", chunks=("a" * 40, "b" * 40, "c"), fail_after=(2, StatusError(500))
    )
    p._bill = MagicMock(return_value=0.05)
    router = make_router({"p": p})
    req = request()
    with pytest.raises(Exception):
        await collect(router.stream(req))
    u = req.stream_usage
    assert u.provider == "p" and not u.completed and u.cost_usd == 0.05
    assert u.output_tokens == 20  # 80 chars delivered
    assert router.stats()["provider_stats"]["p"]["total_cost"] == pytest.approx(0.05)


async def test_client_leaving_early_still_settles():
    p = reporting_provider(chunks=("a", "b", "c"))
    router = make_router({"p": p})
    req = request()
    agen = router.stream(req)
    assert await agen.__anext__() == "a"
    await agen.aclose()
    u = req.stream_usage
    assert u.provider == "p" and not u.completed and u.cost_usd > 0


async def test_stream_that_never_opened_costs_nothing():
    p = make_stream_provider("p", open_error=StatusError(400))
    p._bill = MagicMock(return_value=1.0)
    router = make_router({"p": p})
    req = request()
    with pytest.raises(Exception):
        await collect(router.stream(req))
    p._bill.assert_not_called()
    assert req.stream_usage.provider == ""
    assert router.stats()["provider_stats"]["p"]["total_cost"] == 0


async def test_failover_settles_only_the_provider_that_delivered():
    bad = make_stream_provider("bad", open_error=StatusError(500))
    good = reporting_provider("good", cost=0.3)
    router = make_router({"bad": bad, "good": good})
    req = request()
    await collect(router.stream(req))
    assert req.stream_usage.provider == "good"
    bad._bill.assert_not_called()


async def test_accounting_failure_does_not_break_the_stream():
    p = reporting_provider()
    p._bill = MagicMock(side_effect=RuntimeError("price table gone"))
    router = make_router({"p": p})
    assert await collect(router.stream(request())) == ["ab", "cd"]


async def test_streams_do_not_enter_the_latency_histogram():
    p = reporting_provider()
    router = make_router({"p": p})
    await collect(router.stream(request()))
    text = render_prometheus(router.stats())
    assert "caips_request_duration_seconds" not in text
    assert 'caips_tokens_total{provider="p",model="p-real",type="input"} 11' in text
    # A normal call afterwards adds its latency without disturbing token totals.
    router2 = make_router({"p": make_provider("p")})
    await router2.route(request())
    assert "caips_request_duration_seconds_count" in render_prometheus(router2.stats())


# ----------------------------- provider adapters -----------------------------


async def aiter(items):
    for i in items:
        yield i


def req_with_usage():
    return CompletionRequest(
        messages=[Message(role="user", content="hi")], stream_usage=StreamUsage()
    )


async def test_openai_compatible_requests_usage_and_skips_the_final_empty_chunk():
    from providers.openai import OpenAIProvider

    chunks = [
        NS(choices=[NS(delta=NS(content="he"))], usage=None, model="gpt-x"),
        NS(choices=[NS(delta=NS(content="llo"))], usage=None, model="gpt-x"),
        NS(choices=[], usage=NS(prompt_tokens=5, completion_tokens=2), model="gpt-x"),
    ]
    calls = {}

    async def create(**kw):
        calls.update(kw)
        return aiter(chunks)

    p = OpenAIProvider.__new__(OpenAIProvider)
    p.client = NS(chat=NS(completions=NS(create=create)))
    req = req_with_usage()
    out = [c async for c in p.stream(req)]
    assert out == ["he", "llo"]
    assert calls["stream_options"] == {"include_usage": True}
    u = req.stream_usage
    assert (u.input_tokens, u.output_tokens, u.model, u.reported) == (
        5,
        2,
        "gpt-x",
        True,
    )


@pytest.mark.parametrize(
    "mod,cls", [("grok", "GrokProvider"), ("deepseek", "DeepSeekProvider")]
)
async def test_grok_and_deepseek_report_usage(mod, cls):
    import importlib

    Provider = getattr(importlib.import_module(f"providers.{mod}"), cls)
    chunks = [
        NS(choices=[NS(delta=NS(content="x"))], usage=None, model="m"),
        NS(choices=[], usage=NS(prompt_tokens=3, completion_tokens=4), model="m"),
    ]

    async def create(**kw):
        assert kw["stream_options"] == {"include_usage": True}
        return aiter(chunks)

    p = Provider.__new__(Provider)
    p.client = NS(chat=NS(completions=NS(create=create)))
    req = req_with_usage()
    assert [c async for c in p.stream(req)] == ["x"]
    assert (req.stream_usage.input_tokens, req.stream_usage.output_tokens) == (3, 4)


async def test_anthropic_reads_final_message_usage():
    from providers.anthropic import AnthropicProvider

    class Ctx:
        text_stream = aiter(["a", "b"])

        async def get_final_message(self):
            return NS(usage=NS(input_tokens=8, output_tokens=6), model="claude-x")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    p = AnthropicProvider.__new__(AnthropicProvider)
    p.client = NS(messages=NS(stream=lambda **kw: Ctx()))
    req = req_with_usage()
    assert [c async for c in p.stream(req)] == ["a", "b"]
    u = req.stream_usage
    assert (u.input_tokens, u.output_tokens, u.model) == (8, 6, "claude-x")


async def test_anthropic_missing_usage_does_not_fail_the_stream():
    from providers.anthropic import AnthropicProvider

    class Ctx:
        text_stream = aiter(["a"])

        async def get_final_message(self):
            raise RuntimeError("no message")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    p = AnthropicProvider.__new__(AnthropicProvider)
    p.client = NS(messages=NS(stream=lambda **kw: Ctx()))
    req = req_with_usage()
    assert [c async for c in p.stream(req)] == ["a"]
    assert not req.stream_usage.reported


async def test_gemini_reads_usage_metadata_including_thinking_tokens():
    from providers.gemini import GeminiProvider

    chunks = [
        NS(text="a", usage_metadata=None),
        NS(
            text="b",
            usage_metadata=NS(
                prompt_token_count=9, candidates_token_count=4, thoughts_token_count=3
            ),
        ),
    ]

    async def send(msg):
        return aiter(chunks)

    p = GeminiProvider.__new__(GeminiProvider)
    p._build_messages = lambda r: ([], "hi")
    p._build_config = lambda r: None
    p.client = NS(aio=NS(chats=NS(create=lambda **kw: NS(send_message_stream=send))))
    req = req_with_usage()
    assert [c async for c in p.stream(req)] == ["a", "b"]
    assert (req.stream_usage.input_tokens, req.stream_usage.output_tokens) == (9, 7)


# ------------------------------ budgets over HTTP ------------------------------


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "stream-usage-secret")
    from unittest.mock import patch

    from fastapi import FastAPI

    import api.server as server
    from api.auth import create_access_token
    from api.routes import router as api_router

    app = FastAPI()
    app.include_router(api_router)
    budgets = BudgetManager(
        BudgetLimits(max_cost_usd=0.5, max_requests=10), clock=FakeClock(0.0)
    )
    router = make_router({"p": reporting_provider(cost=0.3)})
    patches = [
        patch.object(server, "get_router", return_value=router),
        patch.object(server, "get_budgets", return_value=budgets),
    ]
    for pt in patches:
        pt.start()
    token = create_access_token("user:s", ["completions:read", "completions:write"])
    client = TestClient(app)
    client.headers.update({"Authorization": f"Bearer {token}"})
    yield client, budgets
    for pt in patches:
        pt.stop()


def _sse_text(body: str) -> str:
    """Concatenate the content of SSE ``data:`` frames (ignores [DONE])."""
    import json as _json

    out = []
    for frame in body.split("\n\n"):
        for line in frame.splitlines():
            if line.startswith("data: ") and line != "data: [DONE]":
                out.append(_json.loads(line[6:]).get("content", ""))
    return "".join(out)


BODY = {"messages": [{"role": "user", "content": "hi"}]}


def test_stream_cost_and_tokens_count_toward_the_budget(api):
    client, budgets = api
    r = client.post("/v1/completions/stream", json=BODY)
    assert r.status_code == 200 and _sse_text(r.text) == "abcd"
    used = budgets.status("user:s")["used"]
    assert used["requests"] == 1  # counted once, not twice
    assert used["cost_usd"] == pytest.approx(0.3)
    assert used["tokens"] == 18


def test_streaming_cannot_dodge_the_cost_limit(api):
    client, _ = api
    assert client.post("/v1/completions/stream", json=BODY).status_code == 200
    assert (
        client.post("/v1/completions/stream", json=BODY).status_code == 200
    )  # 0.6 now
    r = client.post("/v1/completions/stream", json=BODY)
    assert r.status_code == 429 and r.json()["detail"]["limit"] == "cost_usd"
