"""
coreai/errors.py

Classifies provider failures so retry, failover and the circuit breaker can
make decisions from facts (HTTP status, exception type, Retry-After) instead
of guessing from message text.

Classification order:
  1. HTTP status, from ``status_code``, ``code`` or ``response.status_code``
  2. Exception type (timeouts and connection errors, matched by class name so
     this module needs no provider SDK imports)
  3. Message keywords, as a last resort for exceptions that carry neither
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from enum import Enum
from typing import Optional


class ErrorKind(str, Enum):
    RATE_LIMIT = "rate_limit"  # 429
    OVERLOADED = "overloaded"  # 529, 503
    SERVER_ERROR = "server_error"  # other 5xx
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    AUTH = "auth"  # 401, 403
    QUOTA = "quota"  # billing / insufficient quota, 402
    BAD_REQUEST = "bad_request"  # 400, 404, 413, 422 ...
    DEADLINE = "deadline"  # our own request deadline ran out
    CIRCUIT_OPEN = "circuit_open"
    UNKNOWN = "unknown"


# Transient: the same call may succeed if repeated shortly.
RETRYABLE = frozenset(
    {
        ErrorKind.RATE_LIMIT,
        ErrorKind.OVERLOADED,
        ErrorKind.SERVER_ERROR,
        ErrorKind.TIMEOUT,
        ErrorKind.CONNECTION,
    }
)

# Provider-side problem: another provider may well succeed.
FAILOVER = RETRYABLE | {ErrorKind.AUTH, ErrorKind.QUOTA, ErrorKind.CIRCUIT_OPEN}

# Counts against a provider's circuit breaker (its health, not the caller's input).
BREAKER_COUNTED = RETRYABLE | {ErrorKind.AUTH, ErrorKind.QUOTA}


@dataclass(frozen=True)
class ErrorInfo:
    kind: ErrorKind
    status: Optional[int] = None
    retry_after: Optional[float] = None  # seconds the provider asked us to wait

    @property
    def retryable(self) -> bool:
        return self.kind in RETRYABLE

    @property
    def failover(self) -> bool:
        return self.kind in FAILOVER

    @property
    def counts_against_breaker(self) -> bool:
        return self.kind in BREAKER_COUNTED


class ProviderTimeout(TimeoutError):
    """A provider call exceeded its per-attempt timeout."""

    def __init__(self, provider: str, seconds: float):
        super().__init__(f"{provider} timed out after {seconds:.1f}s")
        self.provider = provider
        self.seconds = seconds


class DeadlineExceeded(TimeoutError):
    """The request's overall deadline ran out (across retries and failovers)."""


class CircuitOpenError(RuntimeError):
    """A provider's circuit breaker is open; the call was not attempted."""

    def __init__(self, provider: str, retry_after: Optional[float] = None):
        super().__init__(f"circuit open for provider '{provider}'")
        self.provider = provider
        self.retry_after = retry_after


class RateLimited(RuntimeError):
    """Our own rate limiter could not admit the request within its wait budget."""

    def __init__(self, provider: str, retry_after: Optional[float] = None):
        super().__init__(f"rate limit reached for provider '{provider}'")
        self.provider = provider
        self.retry_after = retry_after


class AllProvidersUnavailable(RuntimeError):
    """Every candidate provider failed or was skipped."""

    def __init__(
        self,
        failures: list[tuple[str, ErrorKind]],
        retry_after: Optional[float] = None,
    ):
        detail = ", ".join(f"{p}={k.value}" for p, k in failures) or "none configured"
        super().__init__(f"all providers unavailable ({detail})")
        self.failures = failures
        self.retry_after = retry_after


def status_of(exc: BaseException) -> Optional[int]:
    """HTTP status carried by a provider exception, if any."""
    return _status_of(exc)


def _status_of(exc: BaseException) -> Optional[int]:
    for attr in ("status_code", "status", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int) and not isinstance(value, bool):
            if 100 <= value <= 599:
                return value
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    if isinstance(value, int) and 100 <= value <= 599:
        return value
    return None


def _retry_after_of(exc: BaseException) -> Optional[float]:
    explicit = getattr(exc, "retry_after", None)
    if isinstance(explicit, (int, float)) and explicit >= 0:
        return float(explicit)
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers is None:
        return None
    try:
        ms = headers.get("retry-after-ms")
        if ms is not None:
            return max(0.0, float(ms) / 1000.0)
        seconds = headers.get("retry-after")
        if seconds is not None:
            try:
                return max(0.0, float(seconds))
            except (TypeError, ValueError):
                # RFC 9110 also permits an HTTP date. Treat a naive parsed
                # date as UTC and convert it to the delay from now.
                retry_at = parsedate_to_datetime(seconds)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError, AttributeError, OverflowError):
        return None
    return None


_QUOTA_HINTS = (
    "insufficient_quota",
    "exceeded your current quota",
    "billing",
    "credit balance",
)


def classify(exc: BaseException) -> ErrorInfo:
    """Classify an exception raised by (or on behalf of) a provider call."""
    if isinstance(exc, DeadlineExceeded):
        return ErrorInfo(ErrorKind.DEADLINE)
    if isinstance(exc, CircuitOpenError):
        return ErrorInfo(ErrorKind.CIRCUIT_OPEN, retry_after=exc.retry_after)
    if isinstance(exc, RateLimited):
        return ErrorInfo(ErrorKind.RATE_LIMIT, retry_after=exc.retry_after)

    status = _status_of(exc)
    retry_after = _retry_after_of(exc)
    message = str(exc).lower()

    if status is not None:
        if status == 429 or status == 402:
            quota = status == 402 or any(h in message for h in _QUOTA_HINTS)
            quota = quota or getattr(exc, "code", None) == "insufficient_quota"
            kind = ErrorKind.QUOTA if quota else ErrorKind.RATE_LIMIT
        elif status in (503, 529):
            kind = ErrorKind.OVERLOADED
        elif status == 408:
            kind = ErrorKind.TIMEOUT
        elif status >= 500:
            kind = ErrorKind.SERVER_ERROR
        elif status in (401, 403):
            kind = ErrorKind.AUTH
        elif 400 <= status < 500:
            kind = ErrorKind.BAD_REQUEST
        else:
            kind = ErrorKind.UNKNOWN
        return ErrorInfo(kind, status=status, retry_after=retry_after)

    names = {cls.__name__.lower() for cls in type(exc).__mro__}
    joined = " ".join(names)
    if isinstance(exc, TimeoutError) or "timeout" in joined:
        return ErrorInfo(ErrorKind.TIMEOUT, retry_after=retry_after)
    if isinstance(exc, ConnectionError) or "connect" in joined:
        return ErrorInfo(ErrorKind.CONNECTION, retry_after=retry_after)

    if "overloaded" in message:
        return ErrorInfo(ErrorKind.OVERLOADED, retry_after=retry_after)
    if any(h in message for h in _QUOTA_HINTS):
        return ErrorInfo(ErrorKind.QUOTA)
    if any(k in message for k in ("rate limit", "429", "too many requests")):
        return ErrorInfo(ErrorKind.RATE_LIMIT, retry_after=retry_after)
    if any(k in message for k in ("timeout", "timed out")):
        return ErrorInfo(ErrorKind.TIMEOUT, retry_after=retry_after)
    if "connection" in message:
        return ErrorInfo(ErrorKind.CONNECTION, retry_after=retry_after)
    return ErrorInfo(ErrorKind.UNKNOWN, retry_after=retry_after)
