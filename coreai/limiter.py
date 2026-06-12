import time
from dataclasses import dataclass
from typing import Optional

from loguru import logger


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
        self.last_refill = time.time()

    def _refill(self):
        now = time.time()
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

        self.active_requests = 0
        self.total_requests = 0
        self.total_waits = 0.0

    async def acquire(self, estimated_tokens: int = 1000) -> float:
        """
        Acquire rate limit slot.
        Returns wait time in seconds (0 if no wait needed).
        """
        # Check concurrent request limit
        while self.active_requests >= self.config.concurrent_requests:
            wait_time = 0.1
            self.total_waits += wait_time
            await self._async_sleep(wait_time)

        # Check request rate limit
        if not self.request_bucket.consume(1):
            wait_time = self.request_bucket.wait_until_available(1)
            self.total_waits += wait_time
            logger.warning(
                f"[{self.name}] Request rate limit hit, waiting {wait_time:.2f}s"
            )
            await self._async_sleep(wait_time)

        # Check token rate limit
        if not self.token_bucket.consume(estimated_tokens):
            wait_time = self.token_bucket.wait_until_available(estimated_tokens)
            self.total_waits += wait_time
            logger.warning(
                f"[{self.name}] Token rate limit hit ({estimated_tokens} tokens), waiting {wait_time:.2f}s"
            )
            await self._async_sleep(wait_time)

        self.active_requests += 1
        self.total_requests += 1
        return self.total_waits

    def release(self):
        """Release rate limit slot after request completes"""
        self.active_requests = max(0, self.active_requests - 1)

    async def _async_sleep(self, seconds: float):
        """Non-blocking sleep"""
        import asyncio
        await asyncio.sleep(seconds)

    def stats(self) -> dict:
        return {
            "provider": self.name,
            "active_requests": self.active_requests,
            "total_requests": self.total_requests,
            "total_wait_time_seconds": round(self.total_waits, 2),
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

    async def acquire(self, provider: str, estimated_tokens: int = 1000) -> float:
        limiter = self.get_limiter(provider)
        if not limiter:
            return 0.0
        return await limiter.acquire(estimated_tokens)

    def release(self, provider: str):
        limiter = self.get_limiter(provider)
        if limiter:
            limiter.release()

    def stats(self) -> dict:
        return {
            "limiters": {name: limiter.stats() for name, limiter in self.limiters.items()}
        }