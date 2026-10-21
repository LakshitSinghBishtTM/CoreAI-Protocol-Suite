"""
coreai/retry.py

Retry with exponential backoff, jitter, Retry-After and a request deadline.

What is retried is decided by coreai.errors.classify (HTTP status and
exception type), not by message text. Only transient failures are retried:
rate limits, overload (429/503/529), 5xx, timeouts and connection errors.
Bad requests, auth and quota errors fail immediately.

Waiting rules for each retry:
  - If the provider sent Retry-After, wait that long, unless it exceeds
    max_wait (then give up on this provider so the router can fail over).
  - Otherwise wait min(base * exponential_base ** (attempt - 1), max_wait),
    with "equal jitter" (half fixed, half random) to avoid thundering herds.
  - If the wait would not fit in the remaining deadline, give up immediately.
"""

import asyncio
import random
from collections import Counter
from typing import Any, Awaitable, Callable, Optional, TypeVar

from loguru import logger

from .deadline import Deadline
from .errors import ErrorInfo, ErrorKind, classify, status_of

T = TypeVar("T")


def should_retry(exc: Exception) -> bool:
    """True for transient errors that are safe to retry."""
    return classify(exc).retryable


def is_rate_limit_error(exc: Exception) -> bool:
    return classify(exc).kind in (ErrorKind.RATE_LIMIT, ErrorKind.QUOTA)


def is_timeout_error(exc: Exception) -> bool:
    return classify(exc).kind in (ErrorKind.TIMEOUT, ErrorKind.CONNECTION)


def compute_wait(
    attempt: int,
    info: ErrorInfo,
    base_wait: float,
    max_wait: float,
    exponential_base: float = 2.0,
    jitter: bool = True,
    rng: Callable[[], float] = random.random,
) -> Optional[float]:
    """Seconds to wait before retry number *attempt* (1-based), or None if the
    provider asked for longer than max_wait and we should not wait for it."""
    if info.retry_after is not None:
        return info.retry_after if info.retry_after <= max_wait else None
    backoff = min(base_wait * (exponential_base ** (attempt - 1)), max_wait)
    if jitter:
        return backoff / 2 + rng() * backoff / 2
    return backoff


async def run_with_retry(
    func: Callable[[], Awaitable[T]],
    *,
    max_attempts: int = 3,
    base_wait: float = 1.0,
    max_wait: float = 30.0,
    exponential_base: float = 2.0,
    jitter: bool = True,
    deadline: Optional[Deadline] = None,
    on_retry: Optional[Callable[[int, Exception, ErrorInfo, float], None]] = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    rng: Callable[[], float] = random.random,
) -> T:
    """Call *func* until it succeeds, a non-retryable error occurs, attempts
    run out, or waiting again would exceed the deadline."""
    attempt = 0
    while True:
        attempt += 1
        try:
            return await func()
        except Exception as exc:
            info = classify(exc)
            if not info.retryable or attempt >= max_attempts:
                raise
            wait = compute_wait(
                attempt, info, base_wait, max_wait, exponential_base, jitter, rng
            )
            if wait is None:
                raise
            if deadline is not None:
                remaining = deadline.remaining()
                if remaining is not None and wait >= remaining:
                    raise
            logger.warning(
                "attempt {}/{} failed (status={} {}: {}); retrying in {:.2f}s",
                attempt,
                max_attempts,
                status_of(exc),
                info.kind.value,
                str(exc)[:100],
                wait,
            )
            if on_retry is not None:
                on_retry(attempt, exc, info, wait)
            await sleep(wait)


async def retry_with_backoff(
    func: Callable[..., Any],
    *args,
    max_attempts: int = 3,
    base_wait: float = 1.0,
    max_wait: float = 30.0,
    **kwargs,
) -> Any:
    """Convenience wrapper: call ``func(*args, **kwargs)`` with retry."""

    async def call():
        return await func(*args, **kwargs)

    return await run_with_retry(
        call, max_attempts=max_attempts, base_wait=base_wait, max_wait=max_wait
    )


class RetryConfig:
    def __init__(
        self,
        max_attempts: int = 3,
        initial_wait_seconds: float = 1.0,
        max_wait_seconds: float = 30.0,
        exponential_base: float = 2.0,
        jitter: bool = True,
    ):
        self.max_attempts = max_attempts
        self.initial_wait_seconds = initial_wait_seconds
        self.max_wait_seconds = max_wait_seconds
        self.exponential_base = exponential_base
        self.jitter = jitter


class RetryManager:
    """Stateful retry helper used by the Router; tracks what was retried."""

    def __init__(self, config: Optional[RetryConfig] = None):
        self.config = config or RetryConfig()
        self.total_retries = 0
        self.total_failures = 0
        self.retries_by_kind: Counter = Counter()
        self.retries_by_status: Counter = Counter()  # HTTP status -> retried attempts

    async def execute(
        self,
        func: Callable[[], Awaitable[T]],
        *,
        deadline: Optional[Deadline] = None,
        max_attempts: Optional[int] = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> T:
        """Run the zero-argument async callable *func* with retry."""

        def note(attempt: int, exc: Exception, info: ErrorInfo, wait: float) -> None:
            self.total_retries += 1
            self.retries_by_kind[info.kind.value] += 1
            status = status_of(exc)
            if status is not None:
                self.retries_by_status[str(status)] += 1

        try:
            return await run_with_retry(
                func,
                max_attempts=max_attempts or self.config.max_attempts,
                base_wait=self.config.initial_wait_seconds,
                max_wait=self.config.max_wait_seconds,
                exponential_base=self.config.exponential_base,
                jitter=self.config.jitter,
                deadline=deadline,
                on_retry=note,
                sleep=sleep,
            )
        except Exception:
            self.total_failures += 1
            raise

    def stats(self) -> dict:
        return {
            "total_retries": self.total_retries,
            "total_failures": self.total_failures,
            "retries_by_kind": dict(self.retries_by_kind),
            "retries_by_status": dict(self.retries_by_status),
        }
