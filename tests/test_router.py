"""
tests/test_router.py

Unit tests for coreai.router — Router, RoutingConfig, RoutingStrategy.
All provider calls are mocked.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from providers.base import CompletionRequest, CompletionResponse, Message
from router import Router, RoutingConfig, RoutingStrategy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_provider(name, cost_per_call=0.0001, latency_ms=200.0):
    p = MagicMock()
    p.name = name
    p.default_model = f"{name}-default"
    p.estimate_cost = MagicMock(return_value=cost_per_call)
    p.complete = AsyncMock(return_value=CompletionResponse(
        content=f"response from {name}",
        model=f"{name}-default",
        provider=name,
        input_tokens=20,
        output_tokens=10,
        cost_usd=cost_per_call,
        latency_ms=latency_ms,
    ))
    return p


def _make_request(content="hello"):
    return CompletionRequest(
        messages=[Message(role="user", content=content)],
        max_tokens=256,
        temperature=0.7,
    )


# ---------------------------------------------------------------------------
# RoutingConfig
# ---------------------------------------------------------------------------

class TestRoutingConfig:

    def test_defaults(self):
        cfg = RoutingConfig()
        assert cfg.strategy == RoutingStrategy.BALANCED
        assert cfg.enable_cache is True
        assert cfg.enable_retry is True

    def test_custom_values(self):
        cfg = RoutingConfig(
            strategy=RoutingStrategy.CHEAPEST,
            enable_cache=False,
            max_retry_attempts=5,
        )
        assert cfg.strategy == RoutingStrategy.CHEAPEST
        assert cfg.enable_cache is False
        assert cfg.max_retry_attempts == 5


# ---------------------------------------------------------------------------
# Router initialization
# ---------------------------------------------------------------------------

class TestRouterInit:

    def test_registers_all_providers(self):
        providers = {
            "openai": _make_provider("openai"),
            "anthropic": _make_provider("anthropic"),
        }
        router = Router(providers, RoutingConfig(enable_retry=False))
        assert len(router.providers) == 2

    def test_cache_created_when_enabled(self):
        router = Router(
            {"openai": _make_provider("openai")},
            RoutingConfig(enable_cache=True, enable_retry=False),
        )
        assert router.cache is not None

    def test_cache_none_when_disabled(self):
        router = Router(
            {"openai": _make_provider("openai")},
            RoutingConfig(enable_cache=False, enable_retry=False),
        )
        assert router.cache is None

    def test_retry_manager_created_when_enabled(self):
        router = Router(
            {"openai": _make_provider("openai")},
            RoutingConfig(enable_cache=False, enable_retry=True),
        )
        assert router.retry_manager is not None

    def test_initial_request_count_zero(self):
        router = Router({"openai": _make_provider("openai")}, RoutingConfig(enable_retry=False))
        assert router.request_count == 0


# ---------------------------------------------------------------------------
# Provider selection strategies
# ---------------------------------------------------------------------------

class TestProviderSelection:

    def _router(self, providers, strategy):
        cfg = RoutingConfig(strategy=strategy, enable_cache=False, enable_retry=False)
        return Router(providers, cfg)

    def test_cheapest_selects_lowest_cost(self):
        providers = {
            "expensive": _make_provider("expensive", cost_per_call=0.01),
            "cheap": _make_provider("cheap", cost_per_call=0.0001),
        }
        router = self._router(providers, RoutingStrategy.CHEAPEST)
        selected = router._select_cheapest(_make_request(), list(providers.keys()))
        assert selected == "cheap"

    def test_round_robin_cycles_through_providers(self):
        providers = {
            "a": _make_provider("a"),
            "b": _make_provider("b"),
            "c": _make_provider("c"),
        }
        router = self._router(providers, RoutingStrategy.ROUND_ROBIN)
        keys = list(providers.keys())
        seen = set()
        for _ in range(len(keys)):
            selected = router._select_round_robin(keys)
            seen.add(selected)
        assert seen == set(keys)

    def test_fastest_returns_a_valid_provider(self):
        providers = {
            "a": _make_provider("a"),
            "b": _make_provider("b"),
        }
        router = self._router(providers, RoutingStrategy.FASTEST)
        result = router._select_fastest(list(providers.keys()))
        assert result in providers

    def test_select_raises_with_no_providers(self):
        router = self._router({}, RoutingStrategy.BALANCED)
        with pytest.raises(ValueError, match="No providers"):
            router._select_provider(_make_request())

    def test_balanced_delegates_to_cheapest(self):
        providers = {
            "pricey": _make_provider("pricey", cost_per_call=0.05),
            "budget": _make_provider("budget", cost_per_call=0.0001),
        }
        router = self._router(providers, RoutingStrategy.BALANCED)
        selected = router._select_provider(_make_request())
        assert selected == "budget"


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------

class TestTokenEstimation:

    def test_estimate_tokens_minimum(self):
        router = Router(
            {"openai": _make_provider("openai")},
            RoutingConfig(enable_cache=False, enable_retry=False),
        )
        req = CompletionRequest(
            messages=[Message(role="user", content="Hi")],
            max_tokens=64,
        )
        n = router._estimate_tokens(req)
        assert n >= 100  # enforced minimum

    def test_estimate_tokens_scales_with_content(self):
        router = Router(
            {"openai": _make_provider("openai")},
            RoutingConfig(enable_cache=False, enable_retry=False),
        )
        short_req = CompletionRequest(
            messages=[Message(role="user", content="a" * 40)],
            max_tokens=64,
        )
        long_req = CompletionRequest(
            messages=[Message(role="user", content="a" * 4000)],
            max_tokens=64,
        )
        assert router._estimate_tokens(long_req) > router._estimate_tokens(short_req)


# ---------------------------------------------------------------------------
# Route() — end-to-end
# ---------------------------------------------------------------------------

class TestRouterRoute:

    @pytest.fixture
    def router(self):
        providers = {
            "openai": _make_provider("openai", cost_per_call=0.0001),
            "anthropic": _make_provider("anthropic", cost_per_call=0.0005),
        }
        cfg = RoutingConfig(
            strategy=RoutingStrategy.CHEAPEST,
            enable_cache=False,
            enable_retry=False,
        )
        return Router(providers, cfg)

    @pytest.mark.asyncio
    async def test_route_returns_completion_response(self, router):
        result = await router.route(_make_request())
        assert isinstance(result, CompletionResponse)
        assert result.content

    @pytest.mark.asyncio
    async def test_route_increments_request_count(self, router):
        await router.route(_make_request())
        await router.route(_make_request())
        assert router.request_count == 2

    @pytest.mark.asyncio
    async def test_route_updates_provider_stats(self, router):
        await router.route(_make_request())
        total = sum(s["requests"] for s in router.provider_stats.values())
        assert total == 1

    @pytest.mark.asyncio
    async def test_route_prefers_requested_provider(self, router):
        result = await router.route(_make_request(), preferred_provider="anthropic")
        assert result.provider == "anthropic"

    @pytest.mark.asyncio
    async def test_route_raises_on_unknown_preferred_provider(self, router):
        with pytest.raises(ValueError, match="not available"):
            await router.route(_make_request(), preferred_provider="cohere")

    @pytest.mark.asyncio
    async def test_route_propagates_provider_exception(self, router):
        router.providers["openai"].complete = AsyncMock(
            side_effect=Exception("upstream timeout")
        )
        router.providers["anthropic"].complete = AsyncMock(
            side_effect=Exception("upstream timeout")
        )
        with pytest.raises(Exception):
            await router.route(_make_request())

    @pytest.mark.asyncio
    async def test_stats_shape(self, router):
        await router.route(_make_request())
        s = router.stats()
        assert "total_requests" in s
        assert "strategy" in s
        assert "provider_stats" in s


# ---------------------------------------------------------------------------
# Cache integration
# ---------------------------------------------------------------------------

class TestRouterCacheIntegration:

    @pytest.fixture
    def cached_router(self):
        providers = {"openai": _make_provider("openai")}
        cfg = RoutingConfig(
            strategy=RoutingStrategy.CHEAPEST,
            enable_cache=True,
            enable_retry=False,
            cache_ttl_seconds=300,
        )
        return Router(providers, cfg)

    @pytest.mark.asyncio
    async def test_second_identical_request_hits_cache(self, cached_router):
        req = _make_request("What is 2+2?")
        await cached_router.route(req)
        await cached_router.route(req)
        # Provider should only be called once; second call served from cache
        call_count = cached_router.providers["openai"].complete.call_count
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_different_requests_do_not_share_cache(self, cached_router):
        await cached_router.route(_make_request("question A"))
        await cached_router.route(_make_request("question B"))
        call_count = cached_router.providers["openai"].complete.call_count
        assert call_count == 2
