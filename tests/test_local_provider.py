"""Phase 5: local OpenAI-compatible servers (LM Studio, Ollama, llama-server, vLLM)."""

import httpx
import pytest
from openai import AsyncOpenAI

from providers.base import CompletionRequest, Message
from providers.local import PRESETS, LocalProvider, build_local_providers
from tests.helpers import make_provider, make_router


def chat_reply(content="hi from local", tool_calls=None, usage=True):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    body = {
        "id": "x", "object": "chat.completion", "created": 1, "model": "llama3",
        "choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls" if tool_calls else "stop"}],
    }  # fmt: skip
    if usage:
        body["usage"] = {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}
    return body


def local(handler, **kw):
    p = LocalProvider("lmstudio", PRESETS["lmstudio"], **kw)
    p.client = AsyncOpenAI(
        api_key="x", base_url=p.base_url, max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )  # fmt: skip
    return p


def req(**kw):
    return CompletionRequest(
        messages=[Message(role="user", content="hello")], model="llama3", **kw
    )


async def test_complete_is_free_and_counts_tokens():
    p = local(lambda r: httpx.Response(200, json=chat_reply()))
    out = await p.complete(req())
    assert out.content == "hi from local" and out.provider == "lmstudio"
    assert (out.input_tokens, out.output_tokens, out.cost_usd) == (7, 3, 0.0)


async def test_missing_usage_is_estimated():
    p = local(lambda r: httpx.Response(200, json=chat_reply(usage=False)))
    out = await p.complete(req())
    assert out.input_tokens >= 1 and out.output_tokens >= 1


async def test_hardware_cost_estimate(monkeypatch):
    p = local(
        lambda r: httpx.Response(200, json=chat_reply()), cost_per_hour_usd=3600.0
    )
    assert p._bill(1, 1, "m", latency_s=2.0) == pytest.approx(2.0)
    assert (await p.complete(req())).cost_usd >= 0.0


async def test_tool_calls_come_back_normalised():
    calls = [
        {
            "id": "c1",
            "type": "function",
            "function": {"name": "f", "arguments": '{"a": 1}'},
        }
    ]
    p = local(lambda r: httpx.Response(200, json=chat_reply("", calls)))
    out = await p.complete(req())
    assert out.metadata["tool_calls"] == [
        {"id": "c1", "name": "f", "arguments": '{"a": 1}'}
    ]
    assert out.metadata["finish_reason"] == "tool_calls"


async def test_discovery_adds_models_and_survives_a_dead_server(monkeypatch):
    p = LocalProvider("ollama", PRESETS["ollama"])
    real = httpx.AsyncClient

    def client_with(handler):
        return lambda **kw: real(transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        client_with(lambda r: httpx.Response(200, json={"data": [{"id": "qwen"}]})),
    )
    assert (
        await p.refresh_models() == ["qwen"]
        and "qwen" in p.models
        and p.default_model == "qwen"
    )

    def boom(r):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "AsyncClient", client_with(boom))
    assert await p.refresh_models() == ["qwen"]  # keeps last known list, no exception


def test_capabilities_default_to_off_and_gate_requests():
    p = LocalProvider(
        "lmstudio", PRESETS["lmstudio"], models=["small", "big"],
        capabilities={"default": {"context_length": 100}, "big": {"tools": True, "context_length": 100000}},
    )  # fmt: skip
    tool = [{"type": "function", "function": {"name": "f"}}]
    small = CompletionRequest(
        messages=[Message(role="user", content="x")], model="small", tools=tool
    )
    big = CompletionRequest(
        messages=[Message(role="user", content="x")], model="big", tools=tool
    )
    assert "tool calling" in p.capability_problem(small)
    assert p.capability_problem(big) is None
    long = CompletionRequest(
        messages=[Message(role="user", content="x" * 4000)], model="small"
    )
    assert "context" in p.capability_problem(long)


def test_build_from_config_and_presets():
    built = build_local_providers(
        {
            "servers": {
                "ollama": {},
                "mine": {"base_url": "http://box:9/v1/", "models": ["m"]},
            }
        }
    )
    assert built["ollama"].base_url == PRESETS["ollama"]
    assert built["mine"].base_url == "http://box:9/v1" and built["mine"].models == {"m"}
    with pytest.raises(ValueError):
        build_local_providers({"servers": {"unknown-thing": {}}})


def test_loader_reads_local_yml(tmp_path, monkeypatch):
    f = tmp_path / "local.yml"
    f.write_text("servers:\n  ollama:\n    models: [llama3]\n")
    monkeypatch.setenv("COREAI_LOCAL_FILE", str(f))
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    from providers import load_providers

    res = load_providers()
    assert "ollama" in res and "openai" in res
    assert res["ollama"].is_local is True


# ------------------------------------------------------------------ routing


def local_mock(name="lm", models=("llama3",), behaviour=None):
    p = make_provider(name, cost=0.0, behaviour=behaviour)
    p.is_local = True
    p.models = set(models)
    p.supports_tools = p.supports_vision = True
    return p


async def test_model_name_routes_to_the_server_that_lists_it():
    cloud, lm = make_provider("cloud"), local_mock()
    r = make_router({"cloud": cloud, "lm": lm})
    out = await r.route(req())
    assert out.provider == "lm" and not cloud.complete.called


async def test_private_requests_never_reach_the_cloud():
    cloud, lm = make_provider("cloud"), local_mock()
    r = make_router({"cloud": cloud, "lm": lm})
    request = CompletionRequest(
        messages=[Message(role="user", content="secret")], metadata={"private": True}
    )
    assert (await r.route(request)).provider == "lm"
    assert not cloud.complete.called


async def test_private_with_local_down_fails_instead_of_leaking():
    cloud = make_provider("cloud")
    lm = local_mock(behaviour=ConnectionError("refused"))
    r = make_router({"lm": lm, "cloud": cloud})
    request = CompletionRequest(
        messages=[Message(role="user", content="secret")], metadata={"private": True}
    )
    with pytest.raises(Exception):
        await r.route(request)
    assert not cloud.complete.called


async def test_private_without_any_local_provider_is_refused():
    r = make_router({"cloud": make_provider("cloud")})
    request = CompletionRequest(
        messages=[Message(role="user", content="s")], metadata={"private": True}
    )
    with pytest.raises(ValueError, match="private"):
        await r.route(request)


async def test_local_first_falls_back_to_cloud_when_server_is_down():
    cloud = make_provider("cloud")
    lm = local_mock(behaviour=ConnectionError("refused"))
    r = make_router({"lm": lm, "cloud": cloud})  # fallback strategy: dict order
    request = CompletionRequest(messages=[Message(role="user", content="hi")])
    out = await r.route(request)
    assert out.provider == "cloud"


def test_private_flag_reaches_the_request_from_the_api():
    from types import SimpleNamespace

    from api.openai_compat import _policy_metadata

    body = SimpleNamespace(metadata={"private": True, "policy": "p"}, policy=None)
    assert _policy_metadata(body) == {"policy": "p", "private": True}
    assert (
        _policy_metadata(SimpleNamespace(metadata={"private": "yes"}, policy=None))
        == {}
    )


async def test_probe_marks_down_and_recovers(monkeypatch):
    p = LocalProvider("lmstudio", PRESETS["lmstudio"])
    state = {"ok": False}
    real = httpx.AsyncClient

    def handler(r):
        if not state["ok"]:
            raise httpx.ConnectError("refused")
        return httpx.Response(200, json={"data": [{"id": "qwen"}]})

    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )  # fmt: skip
    assert await p.probe() is False and p.up is False
    assert "down" in p.capability_problem(req())
    state["ok"] = True
    assert await p.probe() is True and p.up is True
    assert "qwen" in p.models and p.capability_problem(req()) is None
