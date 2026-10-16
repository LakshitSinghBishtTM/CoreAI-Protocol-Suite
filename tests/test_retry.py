"""Retry: what is retried, how long it waits, and when it gives up."""

import pytest

from coreai.deadline import Deadline
from coreai.errors import ErrorInfo, ErrorKind
from coreai.retry import RetryConfig, RetryManager, compute_wait, run_with_retry
from tests.helpers import FakeClock, StatusError


class Recorder:
    """Fake sleep that records requested waits instead of waiting."""

    def __init__(self):
        self.waits = []

    async def __call__(self, seconds):
        self.waits.append(seconds)


def flaky(*outcomes):
    outcomes = list(outcomes)
    calls = []

    async def fn():
        calls.append(1)
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    fn.calls = calls
    return fn


@pytest.mark.asyncio
async def test_529_is_retried_until_success():
    sleep = Recorder()
    fn = flaky(StatusError(529), StatusError(529), "ok")
    assert await run_with_retry(fn, max_attempts=3, sleep=sleep) == "ok"
    assert len(fn.calls) == 3 and len(sleep.waits) == 2


@pytest.mark.asyncio
async def test_500_and_timeouts_are_retried():
    sleep = Recorder()
    fn = flaky(StatusError(500), TimeoutError("t"), "ok")
    assert await run_with_retry(fn, max_attempts=3, sleep=sleep) == "ok"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 404, 422])
async def test_client_and_auth_errors_are_not_retried(status):
    sleep = Recorder()
    fn = flaky(StatusError(status))
    with pytest.raises(StatusError):
        await run_with_retry(fn, max_attempts=3, sleep=sleep)
    assert len(fn.calls) == 1 and sleep.waits == []


@pytest.mark.asyncio
async def test_unknown_errors_are_not_retried():
    fn = flaky(RuntimeError("bug"))
    with pytest.raises(RuntimeError):
        await run_with_retry(fn, max_attempts=3, sleep=Recorder())
    assert len(fn.calls) == 1


@pytest.mark.asyncio
async def test_gives_up_after_max_attempts_and_raises_last_error():
    fn = flaky(StatusError(503), StatusError(503), StatusError(503), "never")
    with pytest.raises(StatusError):
        await run_with_retry(fn, max_attempts=3, sleep=Recorder())
    assert len(fn.calls) == 3


@pytest.mark.asyncio
async def test_honors_retry_after():
    sleep = Recorder()
    fn = flaky(StatusError(429, headers={"retry-after": "4"}), "ok")
    await run_with_retry(fn, max_attempts=3, max_wait=30, sleep=sleep)
    assert sleep.waits == [4.0]


@pytest.mark.asyncio
async def test_does_not_wait_for_a_retry_after_longer_than_max_wait():
    """Better to give up (so the router can fail over) than to sit for 5 min."""
    sleep = Recorder()
    fn = flaky(StatusError(429, headers={"retry-after": "300"}), "ok")
    with pytest.raises(StatusError):
        await run_with_retry(fn, max_attempts=3, max_wait=30, sleep=sleep)
    assert sleep.waits == [] and len(fn.calls) == 1


@pytest.mark.asyncio
async def test_does_not_retry_when_wait_exceeds_remaining_deadline():
    clock = FakeClock()
    deadline = Deadline(2.0, clock=clock)
    sleep = Recorder()
    fn = flaky(StatusError(529, headers={"retry-after": "5"}), "ok")
    with pytest.raises(StatusError):
        await run_with_retry(
            fn, max_attempts=3, max_wait=30, deadline=deadline, sleep=sleep
        )
    assert len(fn.calls) == 1


def test_backoff_grows_exponentially_and_is_capped_without_jitter():
    info = ErrorInfo(ErrorKind.SERVER_ERROR)
    waits = [
        compute_wait(a, info, base_wait=1.0, max_wait=5.0, jitter=False)
        for a in (1, 2, 3, 4, 5)
    ]
    assert waits == [1.0, 2.0, 4.0, 5.0, 5.0]


def test_jitter_stays_between_half_and_full_backoff():
    info = ErrorInfo(ErrorKind.SERVER_ERROR)
    low = compute_wait(3, info, 1.0, 60.0, rng=lambda: 0.0)
    high = compute_wait(3, info, 1.0, 60.0, rng=lambda: 1.0)
    assert (low, high) == (2.0, 4.0)


@pytest.mark.asyncio
async def test_retry_manager_tracks_retries_by_kind():
    mgr = RetryManager(RetryConfig(max_attempts=3, initial_wait_seconds=0.001))
    fn = flaky(StatusError(529), StatusError(500), "ok")
    assert await mgr.execute(fn, sleep=Recorder()) == "ok"
    stats = mgr.stats()
    assert stats["total_retries"] == 2 and stats["total_failures"] == 0
    assert stats["retries_by_kind"] == {"overloaded": 1, "server_error": 1}


@pytest.mark.asyncio
async def test_retry_manager_counts_a_failure_when_exhausted():
    mgr = RetryManager(RetryConfig(max_attempts=2, initial_wait_seconds=0.001))
    fn = flaky(StatusError(529), StatusError(529))
    with pytest.raises(StatusError):
        await mgr.execute(fn, sleep=Recorder())
    assert mgr.stats()["total_failures"] == 1


@pytest.mark.asyncio
async def test_retry_manager_max_attempts_override():
    mgr = RetryManager(RetryConfig(max_attempts=5, initial_wait_seconds=0.001))
    fn = flaky(*[StatusError(529)] * 5)
    with pytest.raises(StatusError):
        await mgr.execute(fn, max_attempts=2, sleep=Recorder())
    assert len(fn.calls) == 2
