import asyncio
import time
from dataclasses import dataclass
from typing import Optional

from loguru import logger

from .errors import RateLimited


@dataclass
class RateLimitConfig:
    """Rate limit configuration per provider"""

    requests_per_minute: int = 100
    tokens_per_minute: int = 90000  # input + output tokens
    concurrent_requests: int = 10


class TokenBucket:
    """Token bucket for rate limiting"""

    def __init__(self, capacity: float, refill_rate: float):
        """
        capacity: max tokens
        refill_rate: tokens per second
        """
        self.capacity = capacity
        self.refill_rate = refill_rate
        self.tokens = capacity
        self.last_refill = time.monotonic()

    def _refill(self):
        now = time.monotonic()
        elapsed = now - self.last_refill
        tokens_to_add = elapsed * self.refill_rate
        self.tokens = min(self.capacity, self.tokens + tokens_to_add)
        self.last_refill = now

    def consume(self, tokens: float) -> bool:
        """Try to consume tokens. Returns True if successful."""
        self._refill()
        if self.tokens >= tokens:
            self.tokens -= tokens
            return True
        return False

    def reserve(self, tokens: float) -> float:
        """
        Deduct *tokens* immediately, even if that makes the balance negative,
        and return how many seconds the caller must wait before the reservation
        is covered. Unlike consume(), the deduction always happens, so waiting
        callers are charged and later callers queue behind them.
        A request larger than the bucket's capacity is clamped to capacity so
        it can never wait forever.
        """
        self._refill()
        self.tokens -= min(tokens, self.capacity)
        if self.tokens >= 0:
            return 0.0
        return -self.tokens / self.refill_rate

    def refund(self, tokens: float) -> None:
        """Undo a reservation (used when the caller gives up waiting)."""
        self.tokens = min(self.capacity, self.tokens + min(tokens, self.capacity))

    def wait_until_available(self, tokens: float) -> float:
        """Wait until tokens are available, return wait time"""
        self._refill()
        if self.tokens >= tokens:
            self.tokens -= tokens
            return 0
        shortage = tokens - self.tokens
        wait_time = shortage / self.refill_rate
        return wait_time


class ProviderLimiter:
    """Rate limiter for a single provider"""

    def __init__(self, name: str, config: RateLimitConfig):
        self.name = name
        self.config = config

        # Request rate: convert per-minute to per-second
        self.request_bucket = TokenBucket(
            capacity=config.requests_per_minute,
            refill_rate=config.requests_per_minute / 60,
        )

        # Token rate: convert per-minute to per-second
        self.token_bucket = TokenBucket(
            capacity=config.tokens_per_minute,
            refill_rate=config.tokens_per_minute / 60,
        )

        self._slots = asyncio.Semaphore(config.concurrent_requests)
        self.active_requests = 0
        self.total_requests = 0
        self.total_waits = 0.0
        self.total_rejected = 0

    async def acquire(
        self, estimated_tokens: int = 1000, max_wait: Optional[float] = None
    ) -> float:
        """
        Admit one request. Returns seconds spent waiting.

        Waits for a concurrency slot, then for request-rate and token-rate
        budget. If admission would take longer than *max_wait* seconds
        (None = wait as long as needed), nothing is consumed and RateLimited
        is raised with a retry_after hint, so the caller can fail over or
        return 429 instead of stalling.
        """
        started = time.monotonic()

        def left() -> Optional[float]:
            if max_wait is None:
                return None
            return max(0.0, max_wait - (time.monotonic() - started))

        budget = left()
        if budget is not None and budget <= 0:
            # wait_for(timeout=0) differs across Python versions; check directly.
            if self._slots.locked():
                self.total_rejected += 1
                raise RateLimited(self.name, retry_after=None)
            await self._slots.acquire()
        else:
            try:
                await asyncio.wait_for(self._slots.acquire(), timeout=budget)
            except asyncio.TimeoutError:
                self.total_rejected += 1
                raise RateLimited(self.name, retry_after=None)

        # From here on we hold a concurrency slot; give it back on any failure.
        reserved_requests = reserved_tokens = False
        try:
            req_wait = self.request_bucket.reserve(1)
            reserved_requests = True
            tok_wait = self.token_bucket.reserve(estimated_tokens)
            reserved_tokens = True
            wait = max(req_wait, tok_wait)

            budget = left()
            if budget is not None and wait > budget:
                self.total_rejected += 1
                raise RateLimited(self.name, retry_after=wait)

            if wait > 0:
                logger.warning(f"[{self.name}] rate limit hit, waiting {wait:.2f}s")
                await self._async_sleep(wait)
        except BaseException:
            if reserved_requests:
                self.request_bucket.refund(1)
            if reserved_tokens:
                self.token_bucket.refund(estimated_tokens)
            self._slots.release()
            raise

        self.active_requests += 1
        self.total_requests += 1
        waited = time.monotonic() - started
        self.total_waits += waited
        return waited

    def release(self):
        """Release the slot taken by acquire() once the request completes."""
        if self.active_requests > 0:
            self.active_requests -= 1
            self._slots.release()

    async def _async_sleep(self, seconds: float):
        """Non-blocking sleep"""
        await asyncio.sleep(seconds)

    def stats(self) -> dict:
        return {
            "provider": self.name,
            "active_requests": self.active_requests,
            "total_requests": self.total_requests,
            "total_wait_time_seconds": round(self.total_waits, 2),
            "total_rejected": self.total_rejected,
            "request_bucket_tokens": round(self.request_bucket.tokens, 2),
            "token_bucket_tokens": round(self.token_bucket.tokens, 2),
        }


class GlobalRateLimiter:
    """Manages rate limits across all providers"""

    def __init__(self):
        self.limiters: dict[str, ProviderLimiter] = {}

    def register_provider(self, name: str, config: RateLimitConfig):
        self.limiters[name] = ProviderLimiter(name, config)
        logger.info(f"Registered rate limiter for {name}")

    def get_limiter(self, provider: str) -> Optional[ProviderLimiter]:
        return self.limiters.get(provider)

    async def acquire(
        self,
        provider: str,
        estimated_tokens: int = 1000,
        max_wait: Optional[float] = None,
    ) -> float:
        limiter = self.get_limiter(provider)
        if not limiter:
            return 0.0
        return await limiter.acquire(estimated_tokens, max_wait=max_wait)

    def release(self, provider: str):
        limiter = self.get_limiter(provider)
        if limiter:
            limiter.release()

    def stats(self) -> dict:
        return {
            "limiters": {
                name: limiter.stats() for name, limiter in self.limiters.items()
            }
        }
