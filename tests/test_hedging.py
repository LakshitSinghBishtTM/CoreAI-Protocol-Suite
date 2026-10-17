"""Hedged requests: a slow primary is raced by the next provider."""

import asyncio

import pytest

from coreai.errors import DeadlineExceeded
from coreai.router import RoutingStrategy
from tests.helpers import (
    StatusError,
    hang_forever,
    make_provider,
    make_router,
    request,
    response,
)


def delayed(name, seconds, fail=None):
    async def call(req):
        await asyncio.sleep(seconds)
        if fail is not None:
            raise fail
        return response(name)

    return call


def router(providers, hedge=0.05, **kw):
    return make_router(
        providers,
        strategy=RoutingStrategy.FALLBACK,
        hedge_after_seconds=hedge,
        **kw,
    )


@pytest.mark.asyncio
async def test_fast_primary_does_not_hedge():
    providers = {"a": make_provider("a"), "b": make_provider("b")}
    r = router(providers)
    resp = await r.route(request())
    assert resp.provider == "a"
    providers["b"].complete.assert_not_called()
    assert r.hedge_count == 0


@pytest.mark.asyncio
async def test_slow_primary_loses_to_hedge_and_is_cancelled():
    cancelled = asyncio.Event()

    async def slow(req):
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    providers = {
        "a": make_provider("a", behaviour=slow),
        "b": make_provider("b"),
    }
    r = router(providers)
    resp = await asyncio.wait_for(r.route(request()), 2)
    assert resp.provider == "b"
    assert resp.metadata["hedged"] is True
    assert cancelled.is_set()
    assert (r.hedge_count, r.hedge_wins) == (1, 1)
    assert r.stats()["hedges"] == {"launched": 1, "won_by_second": 1}
    assert r.breakers["a"].state.value == "closed"  # a cancelled loser isn't a failure
    assert r.limiter.stats()  # limiter slot was released (no hang on next call)
    assert (await asyncio.wait_for(r.route(request()), 2)).provider == "b"


@pytest.mark.asyncio
async def test_primary_wins_after_hedge_launched():
    providers = {
        "a": make_provider("a", behaviour=delayed("a", 0.15)),
        "b": make_provider("b", behaviour=delayed("b", 2)),
    }
    r = router(providers)
    resp = await asyncio.wait_for(r.route(request()), 2)
    assert resp.provider == "a"
    assert (r.hedge_count, r.hedge_wins) == (1, 0)


@pytest.mark.asyncio
async def test_failed_leg_does_not_beat_slow_success():
    providers = {
        "a": make_provider("a", behaviour=delayed("a", 0.3)),
        "b": make_provider("b", behaviour=delayed("b", 0.0, fail=StatusError(503))),
    }
    r = router(providers)
    resp = await asyncio.wait_for(r.route(request()), 3)
    assert resp.provider == "a"


@pytest.mark.asyncio
async def test_both_legs_fail_moves_on_to_next_candidate():
    providers = {
        "a": make_provider("a", behaviour=delayed("a", 0.1, fail=StatusError(503))),
        "b": make_provider("b", behaviour=delayed("b", 0.0, fail=StatusError(503))),
        "c": make_provider("c"),
    }
    r = router(providers, max_retry_attempts=1, max_attempts_before_failover=1)
    resp = await asyncio.wait_for(r.route(request()), 3)
    assert resp.provider == "c"
    assert set(resp.metadata["failed_over_from"]) == {"a", "b"}
    # b was already tried as the hedge, so it isn't tried again.
    assert providers["b"].complete.await_count == 1


@pytest.mark.asyncio
async def test_primary_failing_before_delay_uses_normal_failover():
    providers = {
        "a": make_provider("a", behaviour=StatusError(503)),
        "b": make_provider("b"),
    }
    r = router(
        providers, hedge=5.0, max_retry_attempts=1, max_attempts_before_failover=1
    )
    resp = await r.route(request())
    assert resp.provider == "b"
    assert r.hedge_count == 0


@pytest.mark.asyncio
async def test_no_hedge_when_provider_pinned_model_or_off():
    providers = {
        "a": make_provider("a", behaviour=delayed("a", 0.15)),
        "b": make_provider("b"),
    }
    r = router(providers)
    assert (await r.route(request(), preferred_provider="a")).provider == "a"
    assert (await r.route(request(model="m"))).provider == "a"
    off = make_router(providers, strategy=RoutingStrategy.FALLBACK)
    assert (await off.route(request())).provider == "a"
    assert r.hedge_count == 0


@pytest.mark.asyncio
async def test_open_breaker_on_partner_skips_hedge():
    providers = {
        "a": make_provider("a", behaviour=delayed("a", 0.2)),
        "b": make_provider("b"),
    }
    r = router(providers)
    for _ in range(5):
        r.breakers["b"].record_failure()
    resp = await asyncio.wait_for(r.route(request()), 2)
    assert resp.provider == "a"
    assert r.hedge_count == 0


@pytest.mark.asyncio
async def test_deadline_still_applies_with_hedging():
    providers = {
        "a": make_provider("a", behaviour=hang_forever()),
        "b": make_provider("b", behaviour=hang_forever()),
    }
    r = router(providers, hedge=0.02)
    with pytest.raises(DeadlineExceeded):
        await asyncio.wait_for(r.route(request(), deadline_seconds=0.2), 3)


@pytest.mark.asyncio
async def test_caller_cancellation_cancels_both_legs():
    started = []

    async def hang(req):
        started.append(1)
        await asyncio.sleep(10)

    providers = {
        "a": make_provider("a", behaviour=hang),
        "b": make_provider("b", behaviour=hang),
    }
    r = router(providers, hedge=0.02)
    task = asyncio.create_task(r.route(request()))
    await asyncio.sleep(0.15)
    assert len(started) == 2
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    leftovers = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    assert not leftovers


def test_invalid_hedge_config():
    with pytest.raises(ValueError):
        make_router({"a": make_provider("a")}, hedge_after_seconds=0)


def test_policy_hedge_validation():
    from coreai.policy import PolicyError, parse_policies

    ps = parse_policies({"policies": {"default": {"hedge_after_ms": 800}}})
    assert ps.get("default").hedge_after_ms == 800
    with pytest.raises(PolicyError, match="hedge_after_ms"):
        parse_policies({"policies": {"default": {"hedge_after_ms": 0}}})
