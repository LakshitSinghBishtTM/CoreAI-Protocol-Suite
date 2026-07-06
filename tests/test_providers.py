"""
tests/test_providers.py

Unit tests for providers — BaseProvider, OpenAIProvider, AnthropicProvider,
GeminiProvider, GrokProvider, DeepSeekProvider, and load_providers().

All external SDK calls are mocked — no real API calls are made.
"""

from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from providers.base import (
    BaseProvider,
    CompletionRequest,
    CompletionResponse,
    Message,
)
from providers import load_providers

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_request(content="Hello", model=None, system=None):
    return CompletionRequest(
        messages=[Message(role="user", content=content)],
        model=model,
        system_prompt=system,
        max_tokens=256,
        temperature=0.7,
    )


def make_response(provider="openai", model="gpt-4o-mini"):
    return CompletionResponse(
        content="Test response.",
        model=model,
        provider=provider,
        input_tokens=12,
        output_tokens=8,
        cost_usd=0.0000018,
        latency_ms=284.5,
    )


# ---------------------------------------------------------------------------
# BaseProvider
# ---------------------------------------------------------------------------


class TestBaseProvider:
    """Tests for tracking/stats logic in BaseProvider."""

    class _ConcreteProvider(BaseProvider):
        name = "test"
        default_model = "test-model"

        async def complete(self, request):
            return make_response(provider="test", model="test-model")

        async def stream(self, request):
            yield "chunk"

        def estimate_cost(self, i, o, model):
            return 0.001

        def count_tokens(self, text, model):
            return len(text) // 4

    def test_initial_stats_zero(self):
        p = self._ConcreteProvider(api_key="test-key")
        s = p.stats()
        assert s["total_requests"] == 0
        assert s["total_tokens"] == 0
        assert s["total_cost_usd"] == 0.0

    def test_track_accumulates_stats(self):
        p = self._ConcreteProvider(api_key="test-key")
        resp = make_response()
        resp.input_tokens = 100
        resp.output_tokens = 50
        resp.cost_usd = 0.00025
        p._track(resp)
        p._track(resp)
        s = p.stats()
        assert s["total_requests"] == 2
        assert s["total_tokens"] == 300
        assert s["total_cost_usd"] == pytest.approx(0.0005, rel=1e-4)


# ---------------------------------------------------------------------------
# OpenAIProvider
# ---------------------------------------------------------------------------


class TestOpenAIProvider:

    @pytest.fixture
    def provider(self):
        with patch("providers.openai.AsyncOpenAI"):
            from providers.openai import OpenAIProvider

            return OpenAIProvider(api_key="sk-test-key-openai-fixture")

    def test_name(self, provider):
        assert provider.name == "openai"

    def test_default_model(self, provider):
        assert provider.default_model == "gpt-4o-mini"

    @pytest.mark.asyncio
    async def test_complete_returns_response(self, provider):
        mock_resp = MagicMock()
        mock_resp.choices[0].message.content = "42"
        mock_resp.usage.prompt_tokens = 10
        mock_resp.usage.completion_tokens = 5
        mock_resp.model = "gpt-4o-mini"
        provider.client.chat.completions.create = AsyncMock(return_value=mock_resp)

        result = await provider.complete(make_request("What is 6×7?"))
        assert result.content == "42"
        assert result.provider == "openai"
        assert result.input_tokens == 10

    @pytest.mark.asyncio
    async def test_complete_raises_on_api_error(self, provider):
        provider.client.chat.completions.create = AsyncMock(
            side_effect=Exception("rate limit exceeded")
        )
        with pytest.raises(Exception, match="rate limit exceeded"):
            await provider.complete(make_request())

    def test_estimate_cost_gpt4o(self, provider):
        cost = provider.estimate_cost(1000, 500, "gpt-4o")
        assert cost == pytest.approx(1000 * 0.000005 + 500 * 0.000015)

    def test_estimate_cost_unknown_model_uses_default(self, provider):
        cost = provider.estimate_cost(100, 100, "gpt-unknown-future")
        assert cost > 0

    def test_count_tokens_tiktoken(self, provider):
        n = provider.count_tokens("Hello world this is a test sentence.", "gpt-4o")
        assert n > 0

    def test_build_messages_injects_system_prompt(self, provider):
        req = make_request(system="You are a pirate.")
        msgs = provider._build_messages(req)
        assert msgs[0]["role"] == "system"
        assert msgs[0]["content"] == "You are a pirate."

    def test_build_messages_no_system_if_not_set(self, provider):
        req = make_request()
        msgs = provider._build_messages(req)
        assert all(m["role"] != "system" for m in msgs)


# ---------------------------------------------------------------------------
# AnthropicProvider
# ---------------------------------------------------------------------------


class TestAnthropicProvider:

    @pytest.fixture
    def provider(self):
        with patch("providers.anthropic.sdk.AsyncAnthropic"):
            from providers.anthropic import AnthropicProvider

            return AnthropicProvider(api_key="sk-ant-api03-testkey-fixture")

    def test_name(self, provider):
        assert provider.name == "anthropic"

    @pytest.mark.asyncio
    async def test_complete_returns_response(self, provider):
        mock_resp = MagicMock()
        mock_resp.content[0].text = "Paris"
        mock_resp.usage.input_tokens = 15
        mock_resp.usage.output_tokens = 3
        provider.client.messages.create = AsyncMock(return_value=mock_resp)

        result = await provider.complete(make_request("Capital of France?"))
        assert result.content == "Paris"
        assert result.provider == "anthropic"

    def test_build_messages_strips_system_role(self, provider):
        req = CompletionRequest(
            messages=[
                Message(role="system", content="Be concise."),
                Message(role="user", content="Hi"),
            ],
            max_tokens=64,
        )
        msgs = provider._build_messages(req)
        assert all(m["role"] != "system" for m in msgs)

    def test_build_messages_ensures_first_is_user(self, provider):
        req = make_request()
        msgs = provider._build_messages(req)
        assert msgs[0]["role"] == "user"

    def test_estimate_cost_haiku(self, provider):
        cost = provider.estimate_cost(1000, 500, "claude-haiku-4-5")
        expected = 1000 * 0.0000008 + 500 * 0.000004
        assert cost == pytest.approx(expected)

    def test_count_tokens_approximation(self, provider):
        n = provider.count_tokens("a" * 400, "claude-haiku-4-5")
        assert n == 100


# ---------------------------------------------------------------------------
# GeminiProvider
# ---------------------------------------------------------------------------


class TestGeminiProvider:

    @pytest.fixture
    def provider(self):
        with patch("providers.gemini.genai"):
            from providers.gemini import GeminiProvider

            p = GeminiProvider.__new__(GeminiProvider)
            p.api_key = "AIzaSy-test-gemini-fixture"
            p.total_requests = 0
            p.total_tokens = 0
            p.total_cost = 0.0
            return p

    def test_name(self, provider):
        assert provider.name == "gemini"

    def test_estimate_cost_flash(self, provider):
        from providers.gemini import GEMINI_PRICING

        cost = provider.estimate_cost(1000, 500, "gemini-2.0-flash")
        p = GEMINI_PRICING["gemini-2.0-flash"]
        assert cost == pytest.approx(1000 * p["input"] + 500 * p["output"])

    def test_build_messages_excludes_system(self, provider):
        req = CompletionRequest(
            messages=[
                Message(role="system", content="sys"),
                Message(role="user", content="hello"),
                Message(role="assistant", content="hi"),
                Message(role="user", content="bye"),
            ],
            max_tokens=64,
        )
        history, last = provider._build_messages(req)
        assert last == "bye"
        assert all(m["role"] in ("user", "model") for m in history)

    def test_build_messages_converts_assistant_to_model(self, provider):
        req = CompletionRequest(
            messages=[
                Message(role="user", content="ping"),
                Message(role="assistant", content="pong"),
                Message(role="user", content="again"),
            ],
            max_tokens=64,
        )
        history, _ = provider._build_messages(req)
        roles = [m["role"] for m in history]
        assert "model" in roles
        assert "assistant" not in roles


# ---------------------------------------------------------------------------
# GrokProvider
# ---------------------------------------------------------------------------


class TestGrokProvider:

    @pytest.fixture
    def provider(self):
        with patch("providers.grok.AsyncOpenAI"):
            from providers.grok import GrokProvider

            return GrokProvider(api_key="xai-test-grok-fixture")

    def test_name(self, provider):
        assert provider.name == "grok"

    def test_base_url(self, provider):
        assert provider.base_url == "https://api.x.ai/v1"

    def test_estimate_cost_grok3_mini(self, provider):
        from providers.grok import GROK_PRICING

        cost = provider.estimate_cost(500, 200, "grok-3-mini")
        p = GROK_PRICING["grok-3-mini"]
        assert cost == pytest.approx(500 * p["input"] + 200 * p["output"])

    def test_build_messages_injects_system(self, provider):
        req = make_request(system="You are Grok.")
        msgs = provider._build_messages(req)
        assert msgs[0]["role"] == "system"
        assert msgs[0]["content"] == "You are Grok."


# ---------------------------------------------------------------------------
# DeepSeekProvider
# ---------------------------------------------------------------------------


class TestDeepSeekProvider:

    @pytest.fixture
    def provider(self):
        with patch("providers.deepseek.AsyncOpenAI"):
            from providers.deepseek import DeepSeekProvider

            return DeepSeekProvider(api_key="ds-test-deepseek-fixture")

    def test_name(self, provider):
        assert provider.name == "deepseek"

    def test_default_model(self, provider):
        assert provider.default_model == "deepseek-chat"

    def test_estimate_cost_chat(self, provider):
        from providers.deepseek import DEEPSEEK_PRICING

        cost = provider.estimate_cost(1000, 400, "deepseek-chat")
        p = DEEPSEEK_PRICING["deepseek-chat"]
        assert cost == pytest.approx(1000 * p["input"] + 400 * p["output"])

    def test_build_messages_does_not_duplicate_system(self, provider):
        req = CompletionRequest(
            messages=[
                Message(role="system", content="existing system"),
                Message(role="user", content="hello"),
            ],
            system_prompt="also a system prompt",
            max_tokens=64,
        )
        msgs = provider._build_messages(req)
        system_msgs = [m for m in msgs if m["role"] == "system"]
        assert len(system_msgs) == 1

    def test_count_tokens_minimum_one(self, provider):
        assert provider.count_tokens("", "deepseek-chat") == 1


# ---------------------------------------------------------------------------
# load_providers
# ---------------------------------------------------------------------------


class TestLoadProviders:

    def test_skips_providers_with_no_key(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GROK_API_KEY", raising=False)
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        result = load_providers()
        assert result == {}

    def test_loads_provider_when_key_present(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-load-providers-fixture")
        with patch("providers.openai.OpenAIProvider") as MockProvider:
            MockProvider.return_value = MagicMock()
            result = load_providers(enabled=["openai"])
        assert "openai" in result

    def test_enabled_filter_restricts_loaded_providers(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-filter-fixture")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-filter-fixture")
        with (
            patch("providers.openai.OpenAIProvider", return_value=MagicMock()),
            patch("providers.anthropic.AnthropicProvider", return_value=MagicMock()),
        ):
            result = load_providers(enabled=["openai"])
        assert "anthropic" not in result
