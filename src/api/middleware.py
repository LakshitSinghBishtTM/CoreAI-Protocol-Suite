"""
api/middleware.py

Rate limiting middleware for CoreAI API.
Tracks requests per client (API key or IP) in a sliding 60-second window.
"""

import hashlib
import time
from collections import defaultdict, deque
from typing import Tuple

from fastapi import Request, Response
from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Sliding-window rate limiter.

    Attributes:
        rpm: requests per minute allowed per client key
        _windows: dict mapping client_key → deque of request timestamps
    """

    def __init__(self, app, rpm: int = 60):
        super().__init__(app)
        self.rpm = rpm
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
        response.headers["X-RateLimit-Limit"] = str(self.rpm)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        return response

    # ------------------------------------------------------------------
    # Internal helpers — exposed for unit testing
    # ------------------------------------------------------------------

    def _get_client_key(self, request: Request) -> str:
        """Return a rate-limit key: prefer API key header, fall back to IP."""
        api_key = request.headers.get("X-API-Key")
        if api_key:
            # Hash the whole key: a prefix would make keys sharing one throttle
            # each other. The digest also keeps the key itself out of logs.
            return f"key:{hashlib.sha256(api_key.encode()).hexdigest()[:16]}"
        auth = request.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer ") and auth[7:].strip():
            # Hash, not a prefix: every JWT starts with the same header bytes.
            digest = hashlib.sha256(auth[7:].strip().encode()).hexdigest()[:16]
            return f"bearer:{digest}"
        return f"ip:{request.client.host if request.client else 'unknown'}"

    def _check_rate_limit(self, client_key: str) -> Tuple[bool, float]:
        """
        Record a request and check if the client is within the rpm limit.

        Returns:
            (allowed, retry_after_seconds)
            retry_after is 0.0 when allowed.
        """
        now = time.time()
        cutoff = now - 60.0
        self._sweep(now, cutoff)
        window = self._windows.get(client_key)
        if window is None:
            if len(self._windows) >= self.MAX_CLIENTS:
                # Never grow the identity table past its bound. The periodic
                # sweep above removes expired clients; when all entries are
                # active, fail closed for new identities until one expires.
                return False, 60.0
            window = deque()
            self._windows[client_key] = window

        # Evict timestamps outside the 60-second window
        while window and window[0] <= cutoff:
            window.popleft()

        if len(window) >= self.rpm:
            # retry_after = time until oldest request falls out of window
            retry_after = 60.0 - (now - window[0])
            return False, max(retry_after, 0.0)

        window.append(now)
        return True, 0.0

    SWEEP_INTERVAL_S = 60.0
    MAX_CLIENTS = 10_000

    def _sweep(self, now: float, cutoff: float) -> None:
        """Drop clients with no request in the window, so keys that never
        return don't accumulate. Runs at most once a minute; at capacity, new
        client identities are refused until a sweep frees a slot."""
        last = getattr(self, "_last_sweep", 0.0)
        if now - last < self.SWEEP_INTERVAL_S:
            return
        self._last_sweep = now
        for key in [k for k, w in self._windows.items() if not w or w[-1] <= cutoff]:
            del self._windows[key]

    def _remaining(self, client_key: str) -> int:
        """Return remaining requests allowed in the current window."""
        now = time.time()
        window = self._windows.get(client_key, deque())
        cutoff = now - 60.0
        active = sum(1 for ts in window if ts > cutoff)
        return max(0, self.rpm - active)
