"""The chaos and load scripts, run small enough for the normal suite.

The full matrix is `python scripts/chaos.py` (about 40s) and
`python scripts/loadtest.py` (20,000 requests); CI runs both.
"""

import argparse
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import chaos
import loadtest

from coreai.limiter import RateLimitConfig
from coreai.policy import PolicyError, load_rate_limits, parse_rate_limits
from coreai.router import Router, RoutingConfig, RoutingStrategy
from tests.helpers import make_provider


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["down", "overloaded_529", "slow"])
async def test_gateway_keeps_serving_when_one_provider_fails(fault):
    result = await chaos.run_scenario(
        "alpha", fault, requests=60, concurrency=8, min_success=0.99, max_p95_ms=2000
    )
    assert result.passed, result.failures
    assert result.recovery.served_by.get("alpha", 0) > 0  # traffic came back


@pytest.mark.asyncio
async def test_total_outage_fails_fast_then_recovers():
    result = await chaos.run_total_outage(requests=60, concurrency=8)
    assert result.passed, result.failures
    assert result.during.ok == 0


def test_chaos_detects_a_broken_gateway(monkeypatch):
    """The check must be able to fail: with failover off, a dead primary
    can't be survived."""
    original = chaos.build

    def no_failover(order, seed=1):
        router, fakes = original(order, seed)
        router.config.enable_failover = False
        return router, fakes

    monkeypatch.setattr(chaos, "build", no_failover)
    result = asyncio.run(
        chaos.run_scenario("alpha", "down", 40, 4, min_success=0.99, max_p95_ms=2000)
    )
    assert not result.passed


def small_args(**kw):
    base = dict(
        requests=1600,
        concurrency=20,
        cache_entries=100,
        max_growth_mb=5.0,
        max_error_rate=0.01,
    )
    return argparse.Namespace(**{**base, **kw})


def test_load_test_passes_with_bounded_memory():
    result = asyncio.run(loadtest.run(small_args()))
    assert not result["failures"], result
    cache = result["bounded_structures"]["cache entries"]
    assert cache["size"] <= cache["limit"] == 100


def test_load_test_detects_unbounded_growth(monkeypatch):
    leak: list[bytes] = []
    original = loadtest.build_router

    def leaky(cache_entries):
        router, fakes = original(cache_entries)
        real_route = router.route

        async def route(*a, **k):
            leak.append(b"x" * 20_000)  # retained forever
            return await real_route(*a, **k)

        router.route = route
        return router, fakes

    monkeypatch.setattr(loadtest, "build_router", leaky)
    result = asyncio.run(loadtest.run(small_args(max_growth_mb=3.0)))
    assert any("memory grew" in f for f in result["failures"])


# ---- configurable rate limits (found by the load test) -------------------


def test_bundled_routing_yml_limits_are_read():
    limits = load_rate_limits("config/routing.yml")
    assert limits["openai"].requests_per_minute == 10000
    assert limits["openai"].tokens_per_minute == 2_000_000


def test_router_uses_configured_limits_not_hardcoded_ones():
    providers = {"a": make_provider("a"), "b": make_provider("b")}
    cfg = RoutingConfig(
        strategy=RoutingStrategy.FALLBACK,
        rate_limits={"a": RateLimitConfig(requests_per_minute=5000)},
    )
    router = Router(providers, cfg)
    assert router.limiter.limiters["a"].config.requests_per_minute == 5000
    assert router.limiter.limiters["b"].config.requests_per_minute == 100  # default


@pytest.mark.parametrize(
    "body, fragment",
    [
        ({"rate_limits": {"rpm": 0}}, "positive integers"),
        ({"rate_limits": {"rpm": "fast"}}, "positive integers"),
        ({"rate_limits": {"speed": 1}}, "only rpm, tpm and concurrent"),
    ],
)
def test_invalid_rate_limits_rejected(body, fragment):
    with pytest.raises(PolicyError, match=fragment):
        parse_rate_limits({"providers": {"a": body}})


def test_providers_without_limits_are_omitted():
    assert parse_rate_limits({"providers": {"a": {"enabled": True}}}) == {}
