"""
CoreAI Protocol Suite - Logger Middleware
Structured request/response logging for all API traffic.
Logs to loguru with request ID, timing, token usage, and cost.
"""

import time
import uuid
from typing import Callable

from fastapi import Request, Response
from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp


class RequestLoggerMiddleware(BaseHTTPMiddleware):
    """
    Logs every request with:
      - Unique request ID (X-Request-ID header)
      - Method, path, status code
      - Latency in ms
      - Client IP (X-Forwarded-For aware)
    """

    def __init__(self, app: ASGIApp, exclude_paths: list[str] = None):
        super().__init__(app)
        self.exclude_paths = set(exclude_paths or ["/health", "/metrics"])

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if request.url.path in self.exclude_paths:
            return await call_next(request)

        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())[:8]
        request.state.request_id = request_id

        client_ip = (
            request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or request.client.host
        )

        start = time.perf_counter()

        try:
            response = await call_next(request)
        except Exception as e:
            elapsed = (time.perf_counter() - start) * 1000
            logger.error(
                f"[{request_id}] {request.method} {request.url.path} "
                f"ERROR {elapsed:.1f}ms — {type(e).__name__}: {str(e)[:80]}"
            )
            raise

        elapsed = (time.perf_counter() - start) * 1000
        status = response.status_code
        level = "warning" if status >= 400 else "info"

        getattr(logger, level)(
            f"[{request_id}] {client_ip} {request.method} {request.url.path} "
            f"{status} {elapsed:.1f}ms"
        )

        response.headers["X-Request-ID"] = request_id
        response.headers["X-Response-Time"] = f"{elapsed:.1f}ms"
        return response


def log_completion(
    request_id: str,
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float,
    latency_ms: float,
    cached: bool,
):
    """Structured log line for a completed LLM call."""
    logger.info(
        f"[{request_id}] completion "
        f"provider={provider} model={model} "
        f"in={input_tokens} out={output_tokens} "
        f"cost=${cost_usd:.6f} latency={latency_ms:.0f}ms "
        f"cached={cached}"
    )


def log_agent_event(
    request_id: str,
    agent_id: str,
    event: str,
    detail: str = "",
):
    """Structured log line for agent lifecycle events."""
    logger.info(
        f"[{request_id}] agent={agent_id} event={event}"
        + (f" detail={detail[:120]}" if detail else "")
    )
