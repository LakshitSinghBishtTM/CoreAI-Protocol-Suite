"""
api/middleware.py

Request/response middleware for CoreAI API.
Handles logging, rate limiting, CORS, request ID injection, and timing.

Contact: ops@coreai.com
"""

import logging
import time
import uuid
from typing import Callable

from fastapi import Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Request ID middleware
# ------------------------------------------------------------------


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Injects a unique X-Request-ID into every request and response."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        request.state.request_id = request_id

        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response


# ------------------------------------------------------------------
# Timing middleware
# ------------------------------------------------------------------


class TimingMiddleware(BaseHTTPMiddleware):
    """Adds X-Process-Time header and logs request duration."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        start = time.perf_counter()
        response = await call_next(request)
        duration = (time.perf_counter() - start) * 1000  # ms

        response.headers["X-Process-Time"] = f"{duration:.2f}ms"

        request_id = getattr(request.state, "request_id", "-")
        logger.info(
            "%s %s %d %.2fms [%s]",
            request.method,
            request.url.path,
            response.status_code,
            duration,
            request_id,
        )
        return response


# ------------------------------------------------------------------
# Rate limiting middleware
# ------------------------------------------------------------------


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Sliding window rate limiter keyed on API key or IP.
    Delegates to coreai.limiter for the actual token bucket logic.
    """

    EXEMPT_PATHS = {"/health", "/docs", "/openapi.json", "/redoc"}

    def __init__(self, app, requests_per_minute: int = 60):
        super().__init__(app)
        self.rpm = requests_per_minute
        self._windows: dict = {}

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if request.url.path in self.EXEMPT_PATHS:
            return await call_next(request)

        client_key = self._get_client_key(request)
        allowed, retry_after = self._check_rate_limit(client_key)

        if not allowed:
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={"detail": "Rate limit exceeded"},
                headers={"Retry-After": str(retry_after)},
            )

        response = await call_next(request)
        remaining = self._remaining(client_key)
        response.headers["X-RateLimit-Limit"] = str(self.rpm)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        return response

    def _get_client_key(self, request: Request) -> str:
        api_key = request.headers.get("X-API-Key")
        if api_key:
            return f"key:{api_key[:16]}"
        forwarded = request.headers.get("X-Forwarded-For")
        ip = forwarded.split(",")[0].strip() if forwarded else request.client.host
        return f"ip:{ip}"

    def _check_rate_limit(self, key: str):
        now = time.monotonic()
        window = self._windows.setdefault(key, [])
        # Purge entries older than 60s
        self._windows[key] = [t for t in window if now - t < 60]
        if len(self._windows[key]) >= self.rpm:
            oldest = self._windows[key][0]
            retry_after = int(60 - (now - oldest)) + 1
            return False, retry_after
        self._windows[key].append(now)
        return True, 0

    def _remaining(self, key: str) -> int:
        return max(0, self.rpm - len(self._windows.get(key, [])))


# ------------------------------------------------------------------
# Error handling middleware
# ------------------------------------------------------------------


class ErrorHandlerMiddleware(BaseHTTPMiddleware):
    """Catches unhandled exceptions and returns structured JSON errors."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        try:
            return await call_next(request)
        except Exception as exc:
            request_id = getattr(request.state, "request_id", "-")
            logger.exception(
                "Unhandled exception on %s %s [%s]: %s",
                request.method,
                request.url.path,
                request_id,
                exc,
            )
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={
                    "detail": "Internal server error",
                    "request_id": request_id,
                },
            )


# ------------------------------------------------------------------
# CORS config
# ------------------------------------------------------------------

CORS_CONFIG = dict(
    allow_origins=[
        "https://app.coreai.internal",
        "https://dashboard.coreai.internal",
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key", "X-Request-ID"],
    expose_headers=["X-Request-ID", "X-Process-Time", "X-RateLimit-Remaining"],
)


def register_middleware(app) -> None:
    """Register all middleware on the FastAPI app in correct order."""
    app.add_middleware(ErrorHandlerMiddleware)
    app.add_middleware(TimingMiddleware)
    app.add_middleware(RequestIDMiddleware)
    app.add_middleware(RateLimitMiddleware, requests_per_minute=60)
    app.add_middleware(CORSMiddleware, **CORS_CONFIG)
    logger.info(
        "Middleware registered: ErrorHandler, Timing, RequestID, RateLimit, CORS"
    )
