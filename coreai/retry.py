import asyncio
from typing import Callable, Any, TypeVar

from tenacity import (
    AsyncRetrying,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception,
    RetryError,
)
from loguru import logger

T = TypeVar("T")


def is_rate_limit_error(exc: Exception) -> bool:
    """True when the exception looks like a provider rate-limit response."""
    msg = str(exc).lower()
    return any(kw in msg for kw in ("rate limit", "429", "quota", "too many requests"))


def is_timeout_error(exc: Exception) -> bool:
    """True when the exception looks like a network timeout."""
    msg = str(exc).lower()
    return any(kw in msg for kw in ("timeout", "timed out", "deadline", "connection"))


def should_retry(exc: Exception) -> bool:
    """Return True for transient errors that are safe to retry."""
    return is_rate_limit_error(exc) or is_timeout_error(exc)


# ---------------------------------------------------------------------------
# Module-level convenience used by autonomous_agent.py
# ---------------------------------------------------------------------------


async def retry_with_backoff(
    func: Callable[..., Any],
    *args,
    max_attempts: int = 3,
    base_wait: float = 1.0,
    max_wait: float = 30.0,
    **kwargs,
) -> Any:
    """
    Call *func* with exponential back-off retry on transient errors.

    Only retries when ``should_retry(exc)`` returns True; all other
    exceptions propagate immediately.

    Example::

        response = await retry_with_backoff(
            self.kernel.complete,
            agent_id=self.agent_id,
            messages=self.context.messages,
        )
    """
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await func(*args, **kwargs)
        except Exception as exc:
            if not should_retry(exc):
                raise
            last_exc = exc
            if attempt == max_attempts:
                break
            wait = min(base_wait * (2 ** (attempt - 1)), max_wait)
            logger.warning(
                "retry_with_backoff: attempt %d/%d failed (%s) — retrying in %.1fs",
                attempt,
                max_attempts,
                exc,
                wait,
            )
            await asyncio.sleep(wait)

    raise last_exc  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Class-based manager (used by Router)
# ---------------------------------------------------------------------------


class RetryConfig:
    def __init__(
        self,
        max_attempts: int = 3,
        initial_wait_seconds: float = 1.0,
        max_wait_seconds: float = 60.0,
        exponential_base: float = 2.0,
    ):
        self.max_attempts = max_attempts
        self.initial_wait_seconds = initial_wait_seconds
        self.max_wait_seconds = max_wait_seconds
        self.exponential_base = exponential_base


class RetryManager:
    """Stateful retry manager used by the Router."""

    def __init__(self, config: RetryConfig = None):
        self.config = config or RetryConfig()
        self.total_retries = 0
        self.total_failures = 0

    async def execute(self, func: Callable[..., T], *args, **kwargs) -> T:
        """Execute *func* with configured retry behaviour."""
        attempt = 0
        last_exc: Exception | None = None

        try:
            async for attempt_state in AsyncRetrying(
                stop=stop_after_attempt(self.config.max_attempts),
                wait=wait_exponential(
                    multiplier=self.config.initial_wait_seconds,
                    min=self.config.initial_wait_seconds,
                    max=self.config.max_wait_seconds,
                ),
                retry=retry_if_exception(should_retry),
                reraise=True,
            ):
                with attempt_state:
                    attempt += 1
                    try:
                        result = await func(*args, **kwargs)
                        if attempt > 1:
                            logger.info("Succeeded after %d attempts", attempt)
                        return result
                    except Exception as exc:
                        last_exc = exc
                        if attempt < self.config.max_attempts:
                            wait = self.config.initial_wait_seconds * (
                                self.config.exponential_base ** (attempt - 1)
                            )
                            wait = min(wait, self.config.max_wait_seconds)
                            logger.warning(
                                "Attempt %d failed: %s — retrying in %.1fs",
                                attempt,
                                str(exc)[:100],
                                wait,
                            )
                        raise

        except RetryError:
            self.total_failures += 1
            logger.error("Failed after %d attempts: %s", attempt, str(last_exc)[:100])
            raise last_exc or RuntimeError("Unknown retry failure")

        except Exception:
            # Non-transient error — propagate immediately, still count it
            self.total_failures += 1
            raise

        finally:
            if attempt > 1:
                self.total_retries += attempt - 1

    def stats(self) -> dict:
        return {
            "total_retries": self.total_retries,
            "total_failures": self.total_failures,
        }
