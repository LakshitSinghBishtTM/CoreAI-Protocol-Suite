"""Circuit breaker state machine, driven by a fake clock (no sleeping)."""

import pytest

from coreai.circuit_breaker import CircuitBreaker, CircuitState
from tests.helpers import FakeClock


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def breaker(clock):
    return CircuitBreaker("p", failure_threshold=3, reset_timeout=30.0, clock=clock)


def fail(b, n=1):
    for _ in range(n):
        assert b.allow()
        b.record_failure()


def test_starts_closed_and_allows(breaker):
    assert breaker.state is CircuitState.CLOSED
    assert breaker.allow()


def test_opens_after_threshold_consecutive_failures(breaker):
    fail(breaker, 2)
    assert breaker.state is CircuitState.CLOSED
    fail(breaker, 1)
    assert breaker.state is CircuitState.OPEN
    assert not breaker.allow()
    assert not breaker.would_allow()


def test_a_success_resets_the_failure_count(breaker):
    fail(breaker, 2)
    breaker.allow()
    breaker.record_success()
    fail(breaker, 2)
    assert breaker.state is CircuitState.CLOSED


def test_half_open_after_reset_timeout(breaker, clock):
    fail(breaker, 3)
    clock.advance(29.9)
    assert breaker.state is CircuitState.OPEN
    clock.advance(0.2)
    assert breaker.state is CircuitState.HALF_OPEN


def test_half_open_admits_only_one_probe(breaker, clock):
    fail(breaker, 3)
    clock.advance(31)
    assert breaker.allow()  # the probe
    assert not breaker.allow()  # everyone else waits
    assert not breaker.would_allow()


def test_probe_success_closes_the_circuit(breaker, clock):
    fail(breaker, 3)
    clock.advance(31)
    breaker.allow()
    breaker.record_success()
    assert breaker.state is CircuitState.CLOSED
    assert breaker.allow() and breaker.allow()


def test_probe_failure_reopens_immediately(breaker, clock):
    fail(breaker, 3)
    clock.advance(31)
    breaker.allow()
    breaker.record_failure()
    assert breaker.state is CircuitState.OPEN
    assert breaker.total_opened == 2
    clock.advance(10)
    assert breaker.state is CircuitState.OPEN  # timer restarted


def test_released_probe_slot_can_be_reused(breaker, clock):
    """A probe that ends for unrelated reasons (client left) must not wedge
    the breaker in half-open forever."""
    fail(breaker, 3)
    clock.advance(31)
    assert breaker.allow()
    breaker.release()
    assert breaker.allow()


def test_retry_after_counts_down(breaker, clock):
    assert breaker.retry_after() is None
    fail(breaker, 3)
    assert breaker.retry_after() == pytest.approx(30.0)
    clock.advance(12)
    assert breaker.retry_after() == pytest.approx(18.0)


def test_snapshot(breaker):
    fail(breaker, 3)
    snap = breaker.snapshot()
    assert snap["state"] == "open" and snap["times_opened"] == 1


def test_rejects_bad_threshold():
    with pytest.raises(ValueError):
        CircuitBreaker("p", failure_threshold=0)
