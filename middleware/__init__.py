"""
middleware/__init__.py
"""
from .auth import require_api_key, optional_api_key
from .logger import RequestLoggerMiddleware, log_completion, log_agent_event
from .validator import (
    validate_completion_request,
    validate_messages,
    validate_max_tokens,
    validate_temperature,
    validate_provider,
    ValidationError,
)

# RateLimitMiddleware lives in api/middleware.py. Import only what is
# actually defined there — the test only needs RateLimitMiddleware.
try:
    from api.middleware import RateLimitMiddleware
except ImportError:
    # Fallback: api/ not yet on path at import time; will resolve at test runtime.
    pass

__all__ = [
    "require_api_key",
    "optional_api_key",
    "RequestLoggerMiddleware",
    "log_completion",
    "log_agent_event",
    "validate_completion_request",
    "validate_messages",
    "validate_max_tokens",
    "validate_temperature",
    "validate_provider",
    "ValidationError",
    "RateLimitMiddleware",
]