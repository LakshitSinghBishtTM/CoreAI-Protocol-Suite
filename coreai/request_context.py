"""Request IDs: one per request, carried through logs and the response.

A pure-ASGI middleware (so streaming responses and context variables behave)
reads a client-supplied ``X-Request-ID`` if it looks safe, otherwise makes one,
stores it in a context variable for the logging pipeline, echoes it in the
response header, and writes one structured access line per request.
"""

from __future__ import annotations

import re
import time
import uuid
from contextvars import ContextVar

from loguru import logger

from coreai import tracing

HEADER = b"x-request-id"
_SAFE = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
# Probes and scrapers would drown the access log.
_QUIET_PATHS = ("/health", "/healthz", "/readyz", "/metrics")


def current_request_id() -> str | None:
    return _request_id.get()


def new_request_id() -> str:
    return uuid.uuid4().hex


class RequestContextMiddleware:
    def __init__(self, app, quiet_paths: tuple[str, ...] = _QUIET_PATHS):
        self.app = app
        self.quiet_paths = quiet_paths

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        supplied = dict(scope.get("headers") or []).get(HEADER, b"").decode(
            "latin-1"
        )
        rid = supplied if _SAFE.match(supplied) else new_request_id()
        token = _request_id.set(rid)
        started = time.perf_counter()
        status = {"code": 500}
        method = scope.get("method", "")
        span = tracing.start_span(
            f"{method} {scope.get('path', '')}",
            {
                "http.request.method": method,
                "url.path": scope.get("path", ""),
                "caips.request_id": rid,
            },
            parent=tracing.extract_context(scope.get("headers") or []),
            server=True,
        )

        async def send_with_id(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                headers = [(k, v) for k, v in message.get("headers", []) if k != HEADER]
                headers.append((HEADER, rid.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        except BaseException as exc:
            span.record_error(exc)
            raise
        finally:
            route = scope.get("route")
            if route is not None and getattr(route, "path", None):
                span.update_name(f"{method} {route.path}")  # low-cardinality name
            span.set_attribute("http.response.status_code", status["code"])
            if status["code"] >= 500:
                span.mark_error(f"http_{status['code']}")
            span.end()
            path = scope.get("path", "")
            if not path.startswith(self.quiet_paths):
                code = status["code"]
                log = logger.bind(
                    method=scope.get("method"),
                    path=path,
                    status=code,
                    duration_ms=round((time.perf_counter() - started) * 1000, 1),
                )
                (log.warning if code >= 500 else log.info)("request")
            _request_id.reset(token)
