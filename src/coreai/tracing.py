"""Optional OpenTelemetry tracing.

Off unless asked for, and costless when off: without the packages or the
setting, every function here is a no-op and ``start_span`` returns a shared
do-nothing span. Install the extra with ``pip install 'coreai[tracing]'``.

Enable with ``COREAI_TRACING=1`` or by setting ``OTEL_EXPORTER_OTLP_ENDPOINT``
(or ``OTEL_EXPORTER_OTLP_TRACES_ENDPOINT``); spans go out over OTLP/HTTP using
the standard ``OTEL_EXPORTER_OTLP_*`` variables. Also read:
``OTEL_SERVICE_NAME`` (default ``caips-gateway``) and
``COREAI_TRACE_SAMPLE_RATIO`` (0 to 1, default 1; parent decisions win, so a
caller's ``traceparent`` is honoured).

Privacy: prompts, responses and exception messages are never put on spans.
Errors carry the exception type and the gateway's error kind only.

The tracer provider is private to this module (not installed globally), so
enabling tracing cannot interfere with other OpenTelemetry users in-process.
"""

from __future__ import annotations

import os
import threading
from typing import Any

from loguru import logger

_TRUE = {"1", "true", "yes", "on"}
_tracer: Any = None
_provider: Any = None
_lock = threading.Lock()


def _clean(value: Any) -> Any:
    """Attribute values must be primitives (or lists of them)."""
    if isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value[:50]]
    return str(value)


class _NoopSpan:
    def set_attribute(self, key: str, value: Any) -> None: ...

    def set_attributes(self, attributes: dict) -> None: ...

    def add_event(self, name: str, attributes: dict | None = None) -> None: ...

    def record_error(self, exc: BaseException, kind: str | None = None) -> None: ...

    def mark_error(self, description: str) -> None: ...

    def update_name(self, name: str) -> None: ...

    def end(self) -> None: ...

    def __enter__(self) -> "_NoopSpan":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


_NOOP = _NoopSpan()


class _Span:
    """A live span; optionally the current span until ``end()``."""

    def __init__(self, span: Any, token: Any = None):
        self._span = span
        self._token = token
        self._ended = False

    def set_attribute(self, key: str, value: Any) -> None:
        if value is not None:
            self._span.set_attribute(key, _clean(value))

    def set_attributes(self, attributes: dict) -> None:
        for k, v in attributes.items():
            self.set_attribute(k, v)

    def add_event(self, name: str, attributes: dict | None = None) -> None:
        self._span.add_event(
            name, {k: _clean(v) for k, v in (attributes or {}).items()}
        )

    def record_error(self, exc: BaseException, kind: str | None = None) -> None:
        from opentelemetry.trace import Status, StatusCode

        self._span.add_event("exception", {"exception.type": type(exc).__name__})
        if kind:
            self._span.set_attribute("caips.error_kind", kind)
        self._span.set_status(Status(StatusCode.ERROR, kind or type(exc).__name__))

    def mark_error(self, description: str) -> None:
        """Mark the span failed without an exception (for example an HTTP 5xx)."""
        from opentelemetry.trace import Status, StatusCode

        self._span.set_status(Status(StatusCode.ERROR, description))

    def update_name(self, name: str) -> None:
        self._span.update_name(name)

    def end(self) -> None:
        if self._ended:
            return
        self._ended = True
        if self._token is not None:
            from opentelemetry import context

            try:
                context.detach(self._token)
            except Exception:  # ended from another context: harmless
                pass
        self._span.end()

    def __enter__(self) -> "_Span":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc is not None and not isinstance(exc, GeneratorExit):
            from coreai.errors import classify

            try:
                kind = classify(exc).kind.value
            except Exception:
                kind = None
            self.record_error(exc, kind)
        self.end()
        return None


def enabled() -> bool:
    return _tracer is not None


def wanted() -> bool:
    env = os.environ
    return env.get("COREAI_TRACING", "").lower() in _TRUE or bool(
        env.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        or env.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
    )


def setup_tracing(exporter: Any = None, simple: bool = False) -> bool:
    """Turn tracing on if requested. Returns whether it is now on.

    A missing package or bad setting logs an error and leaves tracing off;
    it never stops the gateway. *exporter* and *simple* are for tests.
    """
    global _tracer, _provider
    if exporter is None and not wanted():
        return False
    try:
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import (
            BatchSpanProcessor,
            SimpleSpanProcessor,
        )
        from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
    except ImportError:
        logger.error(
            "Tracing was requested but OpenTelemetry is not installed. "
            "Install it with: pip install 'coreai[tracing]'"
        )
        return False

    raw = os.environ.get("COREAI_TRACE_SAMPLE_RATIO", "1")
    try:
        ratio = float(raw)
        if not 0.0 <= ratio <= 1.0:
            raise ValueError
    except ValueError:
        logger.error(f"COREAI_TRACE_SAMPLE_RATIO={raw!r} must be 0 to 1; using 1")
        ratio = 1.0

    if exporter is None:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )

            exporter = OTLPSpanExporter()
        except ImportError:
            logger.error(
                "Tracing needs the OTLP exporter: pip install 'coreai[tracing]'"
            )
            return False

    with _lock:
        provider = TracerProvider(
            resource=Resource.create(
                {"service.name": os.environ.get("OTEL_SERVICE_NAME", "caips-gateway")}
            ),
            sampler=ParentBased(TraceIdRatioBased(ratio)),
        )
        # BatchSpanProcessor has a bounded queue and drops on overflow, so a
        # slow collector can never grow memory or slow requests.
        processor = SimpleSpanProcessor if simple else BatchSpanProcessor
        provider.add_span_processor(processor(exporter))
        _provider = provider
        _tracer = provider.get_tracer("coreai")
    logger.info(f"Tracing enabled (sample ratio {ratio:g})")
    return True


def shutdown_tracing(timeout_ms: int = 5000) -> None:
    """Flush queued spans and turn tracing off."""
    global _tracer, _provider
    with _lock:
        provider, _tracer, _provider = _provider, None, None
    if provider is not None:
        try:
            provider.force_flush(timeout_ms)
            provider.shutdown()
        except Exception:
            logger.exception("tracing shutdown failed")


def start_span(
    name: str,
    attributes: dict | None = None,
    *,
    current: bool = True,
    parent: Any = None,
    server: bool = False,
):
    """Start a span. With ``current=True`` it is the current span until it
    ends (use for code that starts and ends in one coroutine or ``with``);
    use ``current=False`` for spans that outlive a generator's yields."""
    if _tracer is None:
        return _NOOP
    from opentelemetry import context, trace

    kwargs: dict[str, Any] = {}
    if parent is not None:
        kwargs["context"] = parent
    if server:
        kwargs["kind"] = trace.SpanKind.SERVER
    span = _tracer.start_span(name, **kwargs)
    wrapped = _Span(span)
    for k, v in (attributes or {}).items():
        wrapped.set_attribute(k, v)
    if current:
        ctx = trace.set_span_in_context(span, parent)
        wrapped._token = context.attach(ctx)
    return wrapped


def current_span():
    """The active span (a no-op span if none or tracing is off)."""
    if _tracer is None:
        return _NOOP
    from opentelemetry import trace

    span = trace.get_current_span()
    return _Span(span) if span.get_span_context().is_valid else _NOOP


def add_event(name: str, attributes: dict | None = None) -> None:
    current_span().add_event(name, attributes)


def current_ids() -> tuple[str, str] | None:
    """(trace_id, span_id) as hex for log correlation, or None."""
    if _tracer is None:
        return None
    from opentelemetry import trace

    ctx = trace.get_current_span().get_span_context()
    if not ctx.is_valid:
        return None
    return format(ctx.trace_id, "032x"), format(ctx.span_id, "016x")


def extract_context(headers: list[tuple[bytes, bytes]]) -> Any:
    """Parent context from a caller's W3C ``traceparent``/``tracestate``."""
    if _tracer is None:
        return None
    from opentelemetry.propagate import extract

    carrier = {
        k.decode("latin-1").lower(): v.decode("latin-1")
        for k, v in headers
        if k.lower() in (b"traceparent", b"tracestate")
    }
    return extract(carrier) if carrier else None
