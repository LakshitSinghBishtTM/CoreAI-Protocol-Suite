"""Per-provider health scoring and its effect on routing."""

import pytest

from coreai.errors import ErrorKind
from coreai.health import HealthTracker
from coreai.router import RoutingStrategy
from tests.helpers import FakeClock, StatusError, make_provider, make_router, request


def test_no_data_is_fully_healthy():
    assert HealthTracker(clock=FakeClock()).score() == 1.0


def test_failures_lower_and_successes_restore_score():
    clock = FakeClock()
    h = HealthTracker(half_life_seconds=60, clock=clock)
    for _ in range(10):
        h.record(False)
    low = h.score()
    assert low < 0.2
    clock.advance(600)  # ten half-lives: old failures barely count
    assert h.score() > 0.99
    for _ in range(10):
        h.record(True)
    assert h.score() > low


def test_single_early_failure_is_not_fatal():
    h = HealthTracker(clock=FakeClock())
    h.record(False)
    assert h.score() == pytest.approx(0.5)


def test_window_is_bounded():
    h = HealthTracker(max_events=5, clock=FakeClock())
    for _ in range(50):
        h.record(True)
    assert h.sample_count() == 5


def test_invalid_half_life():
    with pytest.raises(ValueError):
        HealthTracker(half_life_seconds=0)


def balanced_router(clock=None, **cfg):
    # Equal prices and latency so health alone decides the order.
    providers = {n: make_provider(n, cost=0.001) for n in ("a", "b")}
    return make_router(providers, clock=clock, strategy=RoutingStrategy.BALANCED, **cfg)


def test_unhealthy_provider_is_ranked_behind_healthy_one():
    r = balanced_router(clock=FakeClock())
    for _ in range(6):
        r._record_error("a", ErrorKind.SERVER_ERROR)
    assert r._candidates(request(), None) == ["b", "a"]


def test_health_weight_zero_disables_the_effect():
    r = balanced_router(clock=FakeClock(), health_weight=0.0)
    for _ in range(6):
        r._record_error("a", ErrorKind.SERVER_ERROR)
    assert r._candidates(request(), None)[0] == "a"


def test_bad_requests_do_not_hurt_health():
    r = balanced_router(clock=FakeClock())
    for _ in range(10):
        r._record_error("a", ErrorKind.BAD_REQUEST)
    assert r.health["a"].score() == 1.0


def test_health_recovers_with_time():
    clock = FakeClock()
    r = balanced_router(clock=clock)
    for _ in range(6):
        r._record_error("a", ErrorKind.SERVER_ERROR)
    assert r._candidates(request(), None)[0] == "b"
    clock.advance(900)
    assert r.health["a"].score() > 0.99


@pytest.mark.asyncio
async def test_real_failures_feed_health_and_stats():
    providers = {
        "a": make_provider("a", cost=0.001, behaviour=StatusError(503)),
        "b": make_provider("b", cost=0.001),
    }
    r = make_router(providers, clock=FakeClock(), strategy=RoutingStrategy.FALLBACK)
    resp = await r.route(request())
    assert resp.provider == "b"
    stats = r.stats()["provider_stats"]
    assert stats["a"]["health"] < 1.0
    assert stats["b"]["health"] == 1.0
