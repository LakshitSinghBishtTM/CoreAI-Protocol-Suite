"""Error classification: decisions come from HTTP status and exception type."""

import pytest

from coreai.errors import (
    CircuitOpenError,
    DeadlineExceeded,
    ErrorKind,
    ProviderTimeout,
    RateLimited,
    classify,
)
from tests.helpers import StatusError


@pytest.mark.parametrize(
    "status,kind",
    [
        (429, ErrorKind.RATE_LIMIT),
        (529, ErrorKind.OVERLOADED),  # Anthropic "overloaded_error"
        (503, ErrorKind.OVERLOADED),
        (500, ErrorKind.SERVER_ERROR),
        (502, ErrorKind.SERVER_ERROR),
        (504, ErrorKind.SERVER_ERROR),
        (408, ErrorKind.TIMEOUT),
        (401, ErrorKind.AUTH),
        (403, ErrorKind.AUTH),
        (402, ErrorKind.QUOTA),
        (400, ErrorKind.BAD_REQUEST),
        (404, ErrorKind.BAD_REQUEST),
        (413, ErrorKind.BAD_REQUEST),
        (422, ErrorKind.BAD_REQUEST),
    ],
)
def test_status_codes(status, kind):
    assert classify(StatusError(status)).kind is kind


def test_529_is_retryable_and_fails_over():
    info = classify(StatusError(529, "Overloaded"))
    assert info.retryable and info.failover and info.counts_against_breaker


def test_bad_request_is_never_retried_or_failed_over():
    info = classify(StatusError(400, "invalid parameter"))
    assert not info.retryable and not info.failover
    assert not info.counts_against_breaker  # the caller's fault, not the provider's


def test_auth_and_quota_fail_over_but_are_not_retried():
    for status in (401, 402):
        info = classify(StatusError(status))
        assert not info.retryable
        assert info.failover and info.counts_against_breaker


def test_429_with_quota_message_is_quota_not_rate_limit():
    exc = StatusError(429, "You exceeded your current quota, check your billing")
    assert classify(exc).kind is ErrorKind.QUOTA


def test_retry_after_seconds_header():
    exc = StatusError(429, headers={"retry-after": "7"})
    assert classify(exc).retry_after == 7.0


def test_retry_after_ms_header_wins():
    exc = StatusError(429, headers={"retry-after-ms": "1500", "retry-after": "9"})
    assert classify(exc).retry_after == 1.5


def test_retry_after_http_date_is_converted_to_a_delay():
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime

    retry_at = datetime.now(timezone.utc) + timedelta(seconds=30)
    exc = StatusError(
        503, headers={"retry-after": format_datetime(retry_at, usegmt=True)}
    )
    assert 0 <= classify(exc).retry_after <= 30


def test_expired_retry_after_http_date_means_retry_immediately():
    exc = StatusError(503, headers={"retry-after": "Wed, 21 Oct 2015 07:28:00 GMT"})
    assert classify(exc).retry_after == 0.0


def test_status_on_exception_wins_over_message_text():
    # Message says "timeout" but the status says it was the caller's fault.
    assert classify(StatusError(400, "request timeout param invalid")).kind is (
        ErrorKind.BAD_REQUEST
    )


def test_exception_types_without_status():
    assert classify(TimeoutError("x")).kind is ErrorKind.TIMEOUT
    assert classify(ProviderTimeout("p", 5)).kind is ErrorKind.TIMEOUT
    assert classify(ConnectionResetError("x")).kind is ErrorKind.CONNECTION


def test_sdk_style_class_names_are_recognised():
    class APITimeoutError(Exception):
        pass

    class APIConnectionError(Exception):
        pass

    assert classify(APITimeoutError("x")).kind is ErrorKind.TIMEOUT
    assert classify(APIConnectionError("x")).kind is ErrorKind.CONNECTION


def test_message_keywords_are_last_resort():
    assert classify(RuntimeError("Overloaded")).kind is ErrorKind.OVERLOADED
    assert classify(RuntimeError("rate limit exceeded")).kind is ErrorKind.RATE_LIMIT
    assert classify(RuntimeError("something odd")).kind is ErrorKind.UNKNOWN


def test_unknown_errors_are_not_retried_or_failed_over():
    info = classify(RuntimeError("boom"))
    assert not info.retryable and not info.failover


def test_our_own_exceptions():
    assert classify(DeadlineExceeded()).kind is ErrorKind.DEADLINE
    assert not classify(DeadlineExceeded()).failover
    assert classify(CircuitOpenError("p", 3.0)).kind is ErrorKind.CIRCUIT_OPEN
    assert classify(CircuitOpenError("p", 3.0)).retry_after == 3.0
    assert classify(RateLimited("p", 2.0)).kind is ErrorKind.RATE_LIMIT
