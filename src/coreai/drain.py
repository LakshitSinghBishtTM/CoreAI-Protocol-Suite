"""Kill switch plumbing: stop accepting traffic, then wait for in-flight work.

``DrainController`` is the shared state. ``DrainMiddleware`` is a plain ASGI
middleware (not BaseHTTPMiddleware) so a request counts as in flight until its
response body, including a stream, has been fully sent.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Iterable

# Paths still served while draining, so probes and operators can see state.
_ALWAYS_ALLOWED = ("/health", "/healthz", "/readyz", "/v1/admin/")


class DrainController:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._accepting = True
        self._in_flight = 0
        self._idle = asyncio.Event()
        self._idle.set()
        self.drain_started_at: float | None = None

    @property
    def accepting(self) -> bool:
        return self._accepting

    @property
    def in_flight(self) -> int:
        return self._in_flight

    def begin_drain(self) -> None:
        """Refuse new requests from now on. Idempotent."""
        if self._accepting:
            self._accepting = False
            self.drain_started_at = self._clock()

    def resume(self) -> None:
        """Accept traffic again (only meaningful if the process isn't exiting)."""
        self._accepting = True
        self.drain_started_at = None

    def enter(self) -> None:
        self._in_flight += 1
        self._idle.clear()

    def leave(self) -> None:
        self._in_flight = max(0, self._in_flight - 1)
        if self._in_flight == 0:
            self._idle.set()

    async def wait_idle(self, timeout: float) -> bool:
        """True if every in-flight request finished within *timeout* seconds."""
        try:
            await asyncio.wait_for(self._idle.wait(), timeout=max(0.0, timeout))
            return True
        except TimeoutError:
            return False

    def snapshot(self) -> dict:
        return {"accepting": self._accepting, "in_flight": self._in_flight}


class DrainMiddleware:
    """Rejects new requests with 503 + Retry-After once draining begins."""

    def __init__(
        self,
        app,
        controller: DrainController,
        retry_after: int = 30,
        allowed_prefixes: Iterable[str] = _ALWAYS_ALLOWED,
    ):
        self.app = app
        self.controller = controller
        self.retry_after = retry_after
        self.allowed = tuple(allowed_prefixes)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if not self.controller.accepting and not path.startswith(self.allowed):
            body = json.dumps(
                {"error": "shutting_down", "detail": "Gateway is shutting down."}
            ).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 503,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"retry-after", str(self.retry_after).encode()),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return

        self.controller.enter()
        try:
            await self.app(scope, receive, send)
        finally:
            self.controller.leave()
