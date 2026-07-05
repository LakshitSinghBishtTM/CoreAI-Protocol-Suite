"""
api/middleware.py

Rate limiting middleware for CoreAI API.
Tracks requests per client (API key or IP) in a sliding 60-second window.
"""

import time
from collections import defaultdict, deque
from typing import Optional, Tuple

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from loguru import logger


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Sliding-window rate limiter.

    Attributes:
        rpm: requests per minute allowed per client key
        _windows: dict mapping client_key → deque of request timestamps
    """

    def __init__(self, app, rpm: int = 60):
        super().__init__(app)
        self.rpm      = rpm
        self._windows: dict[str, deque] = defaultdict(deque)

    async def dispatch(self, request: Request, call_next) -> Response:
        client_key = self._get_client_key(request)
        allowed, retry_after = self._check_rate_limit(client_key)

        if not allowed:
            logger.warning(f"Rate limit exceeded for {client_key}")
            return Response(
                content=f"Rate limit exceeded. Retry after {retry_after:.0f}s.",
                status_code=429,
                headers={"Retry-After": str(int(retry_after))},
            )

        response = await call_next(request)
        remaining = self._remaining(client_key)
        response.headers["X-RateLimit-Limit"]     = str(self.rpm)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        return response

    # ------------------------------------------------------------------
    # Internal helpers — exposed for unit testing
    # ------------------------------------------------------------------

    def _get_client_key(self, request: Request) -> str:
        """Return a rate-limit key: prefer API key header, fall back to IP."""
        api_key = request.headers.get("X-API-Key")
        if api_key:
            return f"key:{api_key[:16]}"          # prefix only — don't log full key
        return f"ip:{request.client.host}"

    def _check_rate_limit(self, client_key: str) -> Tuple[bool, float]:
        """
        Record a request and check if the client is within the rpm limit.

        Returns:
            (allowed, retry_after_seconds)
            retry_after is 0.0 when allowed.
        """
        now    = time.time()
        # setdefault (not `self._windows[client_key]`) so this stays correct
        # even when _windows was constructed as a plain dict rather than the
        # defaultdict(deque) __init__ normally sets up (e.g. tests that build
        # the middleware via __new__ and assign `_windows = {}` directly).
        window = self._windows.setdefault(client_key, deque())

        # Evict timestamps outside the 60-second window
        cutoff = now - 60.0
        while window and window[0] <= cutoff:
            window.popleft()

        if len(window) >= self.rpm:
            # retry_after = time until oldest request falls out of window
            retry_after = 60.0 - (now - window[0])
            return False, max(retry_after, 0.0)

        window.append(now)
        return True, 0.0

    def _remaining(self, client_key: str) -> int:
        """Return remaining requests allowed in the current window."""
        now    = time.time()
        window = self._windows.get(client_key, deque())
        cutoff = now - 60.0
        active = sum(1 for ts in window if ts > cutoff)
        return max(0, self.rpm - active)