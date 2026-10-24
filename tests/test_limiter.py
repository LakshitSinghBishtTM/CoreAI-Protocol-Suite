"""Rate limiter: concurrency cap, charging for waits, bounded waiting."""

import asyncio

import pytest

from coreai.errors import RateLimited
from coreai.limiter import ProviderLimiter, RateLimitConfig, TokenBucket


def limiter(**kw):
    cfg = RateLimitConfig(
        requests_per_minute=kw.pop("rpm", 6000),
        tokens_per_minute=kw.pop("tpm", 10_000_000),
        concurrent_requests=kw.pop("concurrent", 10),
    )
    return ProviderLimiter("p", cfg)


@pytest.mark.parametrize(
    "field,value",
    [
        (field, value)
        for field in (
            "requests_per_minute",
            "tokens_per_minute",
            "concurrent_requests",
        )
        for value in (0, -1, 1.5, True)
    ],
)
def test_rate_limit_config_requires_positive_integers(field, value):
    with pytest.raises(ValueError, match="positive integer"):
        RateLimitConfig(**{field: value})


# ------------------------------------------------------------------ TokenBucket


def test_reserve_within_budget_is_free():
    b = TokenBucket(capacity=100, refill_rate=10)
    assert b.reserve(40) == 0.0
    assert b.tokens == pytest.approx(60, abs=1)


def test_reserve_over_budget_returns_wait_and_still_charges():
    b = TokenBucket(capacity=100, refill_rate=10)
    b.reserve(100)
    wait = b.reserve(50)
    assert wait == pytest.approx(5.0, abs=0.2)  # 50 tokens at 10/s
    assert b.tokens < 0  # the debt is recorded so later callers queue behind it


def test_waiters_queue_behind_each_other():
    b = TokenBucket(capacity=100, refill_rate=10)
    b.reserve(100)
    first, second = b.reserve(50), b.reserve(50)
    assert second > first + 4  # not both "5 seconds": they are serialised


def test_refund_restores_tokens_up_to_capacity():
    b = TokenBucket(capacity=100, refill_rate=10)
    b.reserve(60)
    b.refund(60)
    assert b.tokens == pytest.approx(100, abs=1)
    b.refund(500)
    assert b.tokens <= 100


def test_oversized_request_is_clamped_so_it_cannot_wait_forever():
    b = TokenBucket(capacity=100, refill_rate=10)
    assert b.reserve(10_000) == 0.0  # clamped to capacity, bucket was full


# ------------------------------------------------------------------ concurrency


@pytest.mark.asyncio
async def test_concurrency_limit_is_never_exceeded_under_load():
    """No more than `concurrent` requests are ever in flight."""
    lim = limiter(concurrent=3)
    in_flight = peak = 0

    async def worker():
        nonlocal in_flight, peak
        await lim.acquire(10)
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        lim.release()

    await asyncio.gather(*(worker() for _ in range(30)))
    assert peak == 3
    assert lim.active_requests == 0


@pytest.mark.asyncio
async def test_release_without_acquire_does_not_inflate_capacity():
    lim = limiter(concurrent=1)
    lim.release()
    lim.release()
    await lim.acquire(10)
    with pytest.raises(RateLimited):
        await lim.acquire(10, max_wait=0.05)


# ------------------------------------------------------------------ bounded waiting


@pytest.mark.asyncio
async def test_acquire_rejects_instead_of_waiting_past_max_wait():
    lim = limiter(concurrent=1)
    await lim.acquire(10)
    with pytest.raises(RateLimited):
        await lim.acquire(10, max_wait=0.05)
    assert lim.total_rejected == 1


@pytest.mark.asyncio
async def test_rejected_request_does_not_consume_budget_or_a_slot():
    lim = limiter(rpm=60, tpm=1000, concurrent=5)
    await lim.acquire(1000)  # uses the whole token budget
    before = lim.token_bucket.tokens
    with pytest.raises(RateLimited) as info:
        await lim.acquire(500, max_wait=0.05)
    assert info.value.retry_after and info.value.retry_after > 1
    assert lim.token_bucket.tokens == pytest.approx(before, abs=2)  # refunded
    assert lim.active_requests == 1  # only the first request holds a slot
    lim.release()
    assert lim.active_requests == 0


@pytest.mark.asyncio
async def test_token_rate_limit_delays_then_admits():
    lim = limiter(tpm=6000)  # 100 tokens/second
    await lim.acquire(6000)
    waited = await lim.acquire(10, max_wait=2.0)  # needs ~0.1s
    assert 0.05 < waited < 1.0


@pytest.mark.asyncio
async def test_zero_budget_still_admits_when_capacity_is_free():
    lim = limiter()
    await lim.acquire(10, max_wait=0)
    assert lim.active_requests == 1


@pytest.mark.asyncio
async def test_cancelled_waiter_gives_back_its_slot_and_budget():
    lim = limiter(rpm=60, tpm=1000, concurrent=5)
    await lim.acquire(1000)
    task = asyncio.create_task(lim.acquire(1000))  # must wait ~60s
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert lim.active_requests == 1
    lim.release()
    await lim.acquire(10)  # slot is genuinely free


@pytest.mark.asyncio
async def test_requests_that_waited_are_still_charged_to_the_budget():
    """Regression: after sleeping for budget, the old limiter let the request
    through without deducting its tokens, so successive waiters were admitted
    at roughly twice the configured rate."""
    import time

    lim = limiter(tpm=600_000)  # 10,000 tokens/second
    await lim.acquire(600_000)  # drain the bucket
    started = time.monotonic()
    await lim.acquire(1000)  # needs ~0.1s of refill
    await lim.acquire(1000)  # must wait for its own 0.1s, not ride the first
    elapsed = time.monotonic() - started
    assert elapsed >= 0.17  # ~0.2s; the old code finished in ~0.1s
