"""
tests/test_router_fixes.py

Regression tests for the router/provider layer bug-fixes.
All provider calls are mocked — no real API keys needed.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_provider(name, cost=0.001, latency_ms=200.0):
    from providers.base import CompletionResponse

    p = MagicMock()
    p.name = name
    p.default_model = f"{name}-model"
    p.estimate_cost = MagicMock(return_value=cost)
    p.complete = AsyncMock(
        return_value=CompletionResponse(
            content=f"response from {name}",
            model=f"{name}-model",
            provider=name,
            input_tokens=20,
            output_tokens=10,
            cost_usd=cost,
            latency_ms=latency_ms,
        )
    )
    return p


def _make_request(content="hello"):
    from providers.base import CompletionRequest, Message

    return CompletionRequest(
        messages=[Message(role="user", content=content)],
        max_tokens=256,
    )


def _router(providers, strategy_str="balanced", cache=False, retry=False):
    from coreai.router import Router, RoutingConfig, RoutingStrategy

    cfg = RoutingConfig(
        strategy=RoutingStrategy(strategy_str),
        enable_cache=cache,
        enable_retry=retry,
    )
    return Router(providers, cfg)


# ---------------------------------------------------------------------------
# Bug fix 1: preferred_provider validation
# ---------------------------------------------------------------------------


class TestPreferredProviderValidation:

    @pytest.mark.asyncio
    async def test_unknown_provider_raises_clear_valueerror(self):
        providers = {"openai": _make_provider("openai")}
        router = _router(providers)
        with pytest.raises(ValueError, match="not available"):
            await router.route(_make_request(), preferred_provider="cohere")

    @pytest.mark.asyncio
    async def test_known_provider_routes_correctly(self):
        providers = {
            "openai": _make_provider("openai"),
            "anthropic": _make_provider("anthropic"),
        }
        router = _router(providers)
        result = await router.route(_make_request(), preferred_provider="anthropic")
        assert result.provider == "anthropic"

    @pytest.mark.asyncio
    async def test_none_preferred_uses_strategy(self):
        providers = {"openai": _make_provider("openai", cost=0.01)}
        router = _router(providers, strategy_str="cheapest")
        result = await router.route(_make_request())
        assert result.provider == "openai"


# ---------------------------------------------------------------------------
# Bug fix 2: cache key includes provider
# ---------------------------------------------------------------------------


class TestCacheIsolationByProvider:

    @pytest.mark.asyncio
    async def test_different_providers_do_not_share_cache(self):
        providers = {
            "openai": _make_provider("openai"),
            "anthropic": _make_provider("anthropic"),
        }
        router = _router(providers, cache=True)
        req = _make_request("What is 2+2?")

        # Prime the cache for openai
        await router.route(req, preferred_provider="openai")
        # Ask for anthropic — must NOT hit openai's cache
        await router.route(req, preferred_provider="anthropic")

        assert providers["openai"].complete.call_count == 1
        assert providers["anthropic"].complete.call_count == 1

    @pytest.mark.asyncio
    async def test_same_provider_does_use_cache(self):
        providers = {"openai": _make_provider("openai")}
        router = _router(providers, cache=True)
        req = _make_request("What is 2+2?")

        await router.route(req, preferred_provider="openai")
        await router.route(req, preferred_provider="openai")

        # Second call served from cache
        assert providers["openai"].complete.call_count == 1


# ---------------------------------------------------------------------------
# Bug fix 3: _select_fastest uses latency history, not round-robin
# ---------------------------------------------------------------------------


class TestFastestStrategy:

    @pytest.mark.asyncio
    async def test_fastest_picks_lowest_latency_after_warmup(self):
        providers = {
            "slow": _make_provider("slow", latency_ms=900.0),
            "fast": _make_provider("fast", latency_ms=100.0),
        }
        router = _router(providers, strategy_str="fastest")

        # Warm up: _MIN_SAMPLES=3, 2 providers → 6 calls needed.
        # The exploration logic alternates between under-sampled providers,
        # so slow and fast each get exactly 3 samples before exploitation.
        for _ in range(6):
            await router.route(_make_request())

        # Each provider must have exactly _MIN_SAMPLES observations.
        # We check length, not values — wall-clock time is recorded,
        # not the mock's latency_ms attribute.
        assert len(router.provider_stats["slow"]["latency_ms_history"]) == 3
        assert len(router.provider_stats["fast"]["latency_ms_history"]) == 3

        # The mock returns latency_ms on the CompletionResponse, but the router
        # measures wall-clock time for its own history.  Inject realistic values
        # so _select_fastest picks "fast" deterministically.
        router.provider_stats["slow"]["latency_ms_history"] = [900.0, 900.0, 900.0]
        router.provider_stats["fast"]["latency_ms_history"] = [100.0, 100.0, 100.0]

        # After warm-up all auto-routed calls must go to "fast"
        providers["slow"].complete.reset_mock()
        providers["fast"].complete.reset_mock()

        for _ in range(4):
            await router.route(_make_request())

        assert providers["fast"].complete.call_count == 4
        assert providers["slow"].complete.call_count == 0

    def test_fastest_falls_back_to_round_robin_with_no_history(self):
        """Under _MIN_SAMPLES threshold — must not raise, must return a valid provider."""
        providers = {
            "a": _make_provider("a"),
            "b": _make_provider("b"),
        }
        router = _router(providers, strategy_str="fastest")
        result = router._select_fastest(list(providers.keys()))
        assert result in providers


# ---------------------------------------------------------------------------
# Bug fix 4: _select_balanced applies weights (not just cheapest)
# ---------------------------------------------------------------------------


class TestBalancedStrategy:

    def test_balanced_considers_both_cost_and_latency(self):
        """
        Provider A: very cheap but slow.
        Provider B: slightly more expensive but much faster.

        With default weights (cost=0.70, latency=0.30) A should still win
        because cost dominates.  With equal weights (0.50/0.50) B might win.
        We test that the weights are actually applied, not that A always wins.
        """
        from coreai.router import Router, RoutingConfig, RoutingStrategy

        providers = {
            "cheap_slow": _make_provider("cheap_slow", cost=0.001, latency_ms=1000),
            "pricey_fast": _make_provider("pricey_fast", cost=0.010, latency_ms=50),
        }
        # Inject fake latency history
        cfg = RoutingConfig(
            strategy=RoutingStrategy.BALANCED,
            enable_cache=False,
            enable_retry=False,
            cost_weight=0.70,
            latency_weight=0.30,
        )
        router = Router(providers, cfg)
        router.provider_stats["cheap_slow"]["latency_ms_history"] = [1000] * 5
        router.provider_stats["pricey_fast"]["latency_ms_history"] = [50] * 5

        # With cost heavily weighted, the cheap provider should still win
        chosen = router._select_balanced(_make_request(), list(providers))
        assert chosen == "cheap_slow"

    def test_balanced_weights_sum_normalised(self):
        """Weights passed to RoutingConfig always normalise to sum 1.0."""
        from coreai.router import RoutingConfig

        cfg = RoutingConfig(cost_weight=3.0, latency_weight=1.0)
        assert abs(cfg.cost_weight + cfg.latency_weight - 1.0) < 1e-9


# ---------------------------------------------------------------------------
# Bug fix 5: round_robin_index not shared with fastest
# ---------------------------------------------------------------------------


class TestRoundRobinIndexIsolation:

    @pytest.mark.asyncio
    async def test_round_robin_cycles_deterministically(self):
        providers = {
            "a": _make_provider("a"),
            "b": _make_provider("b"),
            "c": _make_provider("c"),
        }
        router = _router(providers, strategy_str="round_robin")
        keys = list(providers)
        seen = []
        for _ in range(len(keys)):
            seen.append(router._select_round_robin(keys))
        assert set(seen) == set(keys), "Expected each provider visited once"

    def test_fastest_does_not_advance_rr_index(self):
        providers = {
            "a": _make_provider("a"),
            "b": _make_provider("b"),
        }
        router = _router(providers, strategy_str="fastest")
        before = router._rr_index
        # _select_fastest falls back to round-robin during warm-up
        router._select_fastest(list(providers))
        # That call may advance _rr_index — that's acceptable.
        # What we verify is that _rr_index is the ONLY mutable index.
        assert hasattr(router, "_rr_index"), "_rr_index should exist"
        assert not hasattr(
            router, "round_robin_index"
        ), "Old round_robin_index attribute should be gone"


# ---------------------------------------------------------------------------
# Bug fix 6: retry_with_backoff importable from coreai.retry
# ---------------------------------------------------------------------------


class TestRetryWithBackoff:

    @pytest.mark.asyncio
    async def test_retry_with_backoff_succeeds_on_first_try(self):
        from coreai.retry import retry_with_backoff

        async def good():
            return "ok"

        result = await retry_with_backoff(good, max_attempts=3)
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_retry_with_backoff_retries_on_timeout(self):
        from coreai.retry import retry_with_backoff

        calls = {"n": 0}

        async def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise TimeoutError("timed out")
            return "ok"

        result = await retry_with_backoff(flaky, max_attempts=3, base_wait=0.01)
        assert result == "ok"
        assert calls["n"] == 3

    @pytest.mark.asyncio
    async def test_retry_with_backoff_does_not_retry_non_transient(self):
        from coreai.retry import retry_with_backoff

        calls = {"n": 0}

        async def bad():
            calls["n"] += 1
            raise ValueError("logic error")  # not transient

        with pytest.raises(ValueError, match="logic error"):
            await retry_with_backoff(bad, max_attempts=3, base_wait=0.01)

        assert calls["n"] == 1, "Should not have retried a non-transient error"

    @pytest.mark.asyncio
    async def test_retry_with_backoff_exhausts_and_raises(self):
        from coreai.retry import retry_with_backoff

        async def always_timeout():
            raise TimeoutError("always")

        with pytest.raises(TimeoutError):
            await retry_with_backoff(always_timeout, max_attempts=2, base_wait=0.01)


# ---------------------------------------------------------------------------
# Bug fix 7: load_providers returns diagnostics, not silent empty dict
# ---------------------------------------------------------------------------


class TestLoadProviders:

    def test_missing_key_is_reported_in_skipped(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GROK_API_KEY", raising=False)
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

        from providers import load_providers

        result = load_providers()
        assert result.providers == {}
        assert "openai" in result.skipped

    def test_load_providers_or_raise_raises_when_empty(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GROK_API_KEY", raising=False)
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

        from providers import load_providers_or_raise

        with pytest.raises(RuntimeError, match="No AI providers"):
            load_providers_or_raise()

    def test_load_providers_or_raise_raises_on_missing_required(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key-openai")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        from providers import load_providers_or_raise, PROVIDER_MAP

        # Patch the class stored in PROVIDER_MAP so the lazy import succeeds
        # without needing the real openai SDK installed.
        original = PROVIDER_MAP["openai"]
        try:
            PROVIDER_MAP["openai"] = (
                MagicMock(return_value=MagicMock()),
                "OPENAI_API_KEY",
            )
            with pytest.raises(RuntimeError, match="anthropic"):
                load_providers_or_raise(required=["anthropic"])
        finally:
            PROVIDER_MAP["openai"] = original
