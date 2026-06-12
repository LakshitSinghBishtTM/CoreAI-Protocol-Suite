import asyncio
from typing import Callable, Any, TypeVar

from tenacity import (
    AsyncRetrying,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    RetryError,
)
from loguru import logger

T = TypeVar("T")

# Retryable exceptions from various providers
RETRYABLE_EXCEPTIONS = (
    # OpenAI
    Exception,  # Broad catch for rate limits, timeouts, etc
)


class RetryConfig:
    def __init__(
        self,
        max_attempts: int = 3,
        initial_wait_seconds: float = 1,
        max_wait_seconds: float = 60,
        exponential_base: float = 2,
    ):
        self.max_attempts = max_attempts
        self.initial_wait_seconds = initial_wait_seconds
        self.max_wait_seconds = max_wait_seconds
        self.exponential_base = exponential_base


class RetryManager:
    def __init__(self, config: RetryConfig = None):
        self.config = config or RetryConfig()
        self.total_retries = 0
        self.total_failures = 0

    async def execute(
        self,
        func: Callable[..., T],
        *args,
        **kwargs,
    ) -> T:
        """Execute function with retry logic"""
        retry_config = AsyncRetrying(
            stop=stop_after_attempt(self.config.max_attempts),
            wait=wait_exponential(
                multiplier=self.config.initial_wait_seconds,
                min=self.config.initial_wait_seconds,
                max=self.config.max_wait_seconds,
            ),
            retry=retry_if_exception_type(RETRYABLE_EXCEPTIONS),
            reraise=True,
        )

        attempt = 0
        last_exception = None

        try:
            async for attempt_state in retry_config:
                with attempt_state:
                    attempt += 1
                    try:
                        logger.debug(f"Attempt {attempt}/{self.config.max_attempts}")
                        result = await func(*args, **kwargs)
                        if attempt > 1:
                            logger.info(f"Succeeded after {attempt} attempts")
                        return result
                    except Exception as e:
                        last_exception = e
                        if attempt < self.config.max_attempts:
                            wait_time = self.config.initial_wait_seconds * (
                                self.config.exponential_base ** (attempt - 1)
                            )
                            wait_time = min(wait_time, self.config.max_wait_seconds)
                            logger.warning(
                                f"Attempt {attempt} failed: {str(e)[:100]}. "
                                f"Retrying in {wait_time:.1f}s..."
                            )
                        raise

        except RetryError as e:
            self.total_failures += 1
            logger.error(f"Failed after {attempt} attempts: {str(last_exception)[:100]}")
            raise last_exception or e

        except Exception as e:
            # Non-retryable exception
            self.total_failures += 1
            logger.error(f"Non-retryable error: {str(e)[:100]}")
            raise

        finally:
            if attempt > 1:
                self.total_retries += attempt - 1

    def stats(self) -> dict:
        return {
            "total_retries": self.total_retries,
            "total_failures": self.total_failures,
        }


def is_rate_limit_error(exception: Exception) -> bool:
    """Check if exception is a rate limit error"""
    error_str = str(exception).lower()
    return any(
        phrase in error_str
        for phrase in ["rate limit", "429", "quota", "too many requests"]
    )


def is_timeout_error(exception: Exception) -> bool:
    """Check if exception is a timeout error"""
    error_str = str(exception).lower()
    return any(
        phrase in error_str
        for phrase in ["timeout", "timed out", "deadline", "connection"]
    )


def should_retry(exception: Exception) -> bool:
    """Determine if exception is retryable"""
    return is_rate_limit_error(exception) or is_timeout_error(exception)