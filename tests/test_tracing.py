"""OpenTelemetry tracing: off by default, spans, failover events, privacy."""

import asyncio
import io
import json
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from loguru import logger

pytest.importorskip("opentelemetry.sdk")
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)
from opentelemetry.trace import SpanKind, StatusCode  # noqa: E402

from coreai import tracing  # noqa: E402
from coreai.logging_setup import configure_logging  # noqa: E402
from coreai.request_context import RequestContextMiddleware  # noqa: E402
from tests.helpers import (  # noqa: E402
    StatusError,
    collect,
    make_provider,
    make_router,
    make_stream_provider,
    request,
)

SECRET_PROMPT = "TOP SECRET PROMPT sk-abcdefghijklmnopqrstuv"


@pytest.fixture
def spans():
    exporter = InMemorySpanExporter()
    assert tracing.setup_tracing(exporter=exporter, simple=True)
    yield exporter
    tracing.shutdown_tracing()


def by_name(exporter, name):
    return [s for s in exporter.get_finished_spans() if s.name == name]


def every_string(exporter):
    out = []
    for s in exporter.get_finished_spans():
        out += [s.name, str(s.status.description or "")]
        out += [str(v) for v in s.attributes.values()]
        for e in s.events:
            out += [e.name] + [str(v) for v in (e.attributes or {}).values()]
    return " ".join(out)


# ------------------------------- off by default -------------------------------


def test_off_by_default_and_costless(monkeypatch):
    for var in ("COREAI_TRACING", "OTEL_EXPORTER_OTLP_ENDPOINT"):
        monkeypatch.delenv(var, raising=False)
    assert tracing.setup_tracing() is False and not tracing.enabled()
    span = tracing.start_span("x", {"a": 1})
    with span as s:
        s.set_attribute("k", "v")
        s.add_event("e")
        s.record_error(RuntimeError("x"), "kind")
    assert tracing.current_ids() is None and tracing.extract_context([]) is None
    assert tracing.start_span("y") is span  # one shared no-op object


async def test_router_unchanged_when_off():
    router = make_router({"p": make_provider("p")})
    assert (await router.route(request())).provider == "p"
    assert await collect(make_router({"s": make_stream_provider("s")}).stream(request()))


def test_requested_by_env(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    assert tracing.wanted()
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    monkeypatch.setenv("COREAI_TRACING", "on")
    assert tracing.wanted()


def test_missing_packages_log_an_error_and_stay_off(monkeypatch):
    seen = []
    sink = logger.add(lambda m: seen.append(str(m)), level="ERROR")
    monkeypatch.setitem(sys.modules, "opentelemetry.sdk.resources", None)
    try:
        assert tracing.setup_tracing(exporter=object()) is False
    finally:
        logger.remove(sink)
    assert any("not installed" in m for m in seen) and not tracing.enabled()


def test_bad_sample_ratio_falls_back_to_one(monkeypatch):
    monkeypatch.setenv("COREAI_TRACE_SAMPLE_RATIO", "banana")
    exporter = InMemorySpanExporter()
    assert tracing.setup_tracing(exporter=exporter, simple=True)
    try:
        tracing.start_span("s").end()
        assert len(exporter.get_finished_spans()) == 1
    finally:
        tracing.shutdown_tracing()


def test_shutdown_turns_tracing_off():
    exporter = InMemorySpanExporter()
    tracing.setup_tracing(exporter=exporter, simple=True)
    tracing.start_span("a").end()
    tracing.shutdown_tracing()
    assert not tracing.enabled() and len(exporter.get_finished_spans()) in (0, 1)
    assert tracing.start_span("b") is tracing.start_span("c")


# ---------------------------------- router ----------------------------------


async def test_route_span_with_failover_and_child_provider_calls(spans):
    router = make_router(
        {
            "bad": make_provider("bad", behaviour=StatusError(500)),
            "good": make_provider("good"),
        }
    )
    await router.route(request(SECRET_PROMPT))

    (route,) = by_name(spans, "caips.route")
    a = route.attributes
    assert a["caips.provider"] == "good" and a["caips.model"] == "good-model"
    assert a["caips.input_tokens"] == 20 and a["caips.output_tokens"] == 10
    assert list(a["caips.failed_over_from"]) == ["bad"]
    (ev,) = [e for e in route.events if e.name == "failover"]
    assert ev.attributes["from"] == "bad" and ev.attributes["to"] == "good"

    calls = by_name(spans, "caips.provider_call")
    assert {c.attributes["caips.provider"] for c in calls} == {"bad", "good"}
    assert all(c.parent.span_id == route.context.span_id for c in calls)
    assert len({c.context.trace_id for c in calls + [route]}) == 1
    failed = [c for c in calls if c.attributes["caips.provider"] == "bad"][0]
    assert failed.status.status_code is StatusCode.ERROR
    assert failed.attributes["caips.error_kind"]
    ok = [c for c in calls if c.attributes["caips.provider"] == "good"][0]
    assert ok.status.status_code is StatusCode.UNSET


async def test_failed_request_marks_the_route_span_without_leaking_text(spans):
    router = make_router(
        {"p": make_provider("p", behaviour=StatusError(400, f"bad input {SECRET_PROMPT}"))}
    )
    with pytest.raises(Exception):
        await router.route(request(SECRET_PROMPT))
    (route,) = by_name(spans, "caips.route")
    assert route.status.status_code is StatusCode.ERROR
    assert any(e.name == "exception" for e in route.events)
    text = every_string(spans)
    assert "TOP SECRET" not in text and "sk-abcdef" not in text


async def test_prompts_and_responses_never_reach_spans(spans):
    router = make_router({"p": make_provider("p")})
    await router.route(request(SECRET_PROMPT))
    assert "TOP SECRET" not in every_string(spans)


async def test_stream_span_carries_settled_usage(spans):
    p = make_stream_provider("p", chunks=("a" * 40,))
    router = make_router({"p": p})
    await collect(router.stream(request("x" * 80)))
    (s,) = by_name(spans, "caips.stream")
    a = s.attributes
    assert a["caips.provider"] == "p" and a["caips.stream_completed"] is True
    assert a["caips.usage_estimated"] is True and a["caips.output_tokens"] == 10
    assert s.status.status_code is StatusCode.UNSET


async def test_stream_failover_event_lands_on_the_stream_span(spans):
    router = make_router(
        {
            "bad": make_stream_provider("bad", open_error=StatusError(500)),
            "good": make_stream_provider("good"),
        }
    )
    await collect(router.stream(request()))
    (s,) = by_name(spans, "caips.stream")
    (ev,) = [e for e in s.events if e.name == "failover"]
    assert ev.attributes["from"] == "bad" and ev.attributes["to"] == "good"


async def test_stream_failing_midway_is_an_error_span(spans):
    p = make_stream_provider("p", chunks=("a", "b"), fail_after=(1, StatusError(500)))
    router = make_router({"p": p})
    with pytest.raises(Exception):
        await collect(router.stream(request()))
    (s,) = by_name(spans, "caips.stream")
    assert s.status.status_code is StatusCode.ERROR
    assert s.attributes["caips.stream_completed"] is False


async def test_client_leaving_early_ends_the_span_without_an_error(spans):
    router = make_router({"p": make_stream_provider("p", chunks=("a", "b", "c"))})
    agen = router.stream(request())
    await agen.__anext__()
    await agen.aclose()
    (s,) = by_name(spans, "caips.stream")
    assert s.status.status_code is StatusCode.UNSET
    assert s.attributes["caips.stream_completed"] is False
    assert s.end_time is not None


async def test_early_close_still_releases_the_provider(spans):
    """The wrapper must close the inner stream itself, not leave it to GC."""
    router = make_router({"p": make_stream_provider("p", chunks=("a", "b"))})
    agen = router.stream(request())
    await agen.__anext__()
    await agen.aclose()
    # A fresh stream must be admitted: the rate-limit slot came back.
    assert await collect(router.stream(request())) == ["a", "b"]


# ------------------------------ HTTP and logging ------------------------------


@pytest.fixture
def app_client(spans):
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)

    @app.get("/items/{item_id}")
    async def item(item_id: int):
        logger.info("in handler")
        return {"id": item_id}

    @app.get("/boom")
    async def boom():
        raise RuntimeError("secret internal detail")

    @app.get("/fail")
    async def fail():
        from fastapi import HTTPException

        raise HTTPException(status_code=503)

    return TestClient(app, raise_server_exceptions=False)


def test_http_server_span_has_route_template_and_status(app_client, spans):
    r = app_client.get("/items/123")
    (s,) = [x for x in spans.get_finished_spans() if x.kind is SpanKind.SERVER]
    assert s.name == "GET /items/{item_id}"  # not one span name per id
    assert s.attributes["http.response.status_code"] == 200
    assert s.attributes["url.path"] == "/items/123"
    assert s.attributes["caips.request_id"] == r.headers["x-request-id"]


def test_5xx_marks_the_span_failed(app_client, spans):
    app_client.get("/fail")
    (s,) = spans.get_finished_spans()
    assert s.status.status_code is StatusCode.ERROR
    assert s.attributes["http.response.status_code"] == 503


def test_unhandled_exception_is_recorded_without_its_message(app_client, spans):
    app_client.get("/boom")
    (s,) = spans.get_finished_spans()
    assert s.status.status_code is StatusCode.ERROR
    assert "secret internal detail" not in every_string(spans)


TRACE_ID = "0af7651916cd43dd8448eb211c80319c"


def test_incoming_traceparent_is_continued(app_client, spans):
    app_client.get(
        "/items/1", headers={"traceparent": f"00-{TRACE_ID}-b7ad6b7169203331-01"}
    )
    (s,) = spans.get_finished_spans()
    assert format(s.context.trace_id, "032x") == TRACE_ID
    assert format(s.parent.span_id, "016x") == "b7ad6b7169203331"


def test_garbage_traceparent_starts_a_new_trace(app_client, spans):
    r = app_client.get("/items/1", headers={"traceparent": "not-a-header"})
    assert r.status_code == 200
    (s,) = spans.get_finished_spans()
    assert s.parent is None


def test_sample_ratio_zero_drops_new_traces_but_honours_sampled_parents(monkeypatch):
    monkeypatch.setenv("COREAI_TRACE_SAMPLE_RATIO", "0")
    exporter = InMemorySpanExporter()
    tracing.setup_tracing(exporter=exporter, simple=True)
    try:
        app = FastAPI()
        app.add_middleware(RequestContextMiddleware)

        @app.get("/x")
        async def x():
            return {}

        c = TestClient(app)
        c.get("/x")
        assert exporter.get_finished_spans() == ()
        c.get("/x", headers={"traceparent": f"00-{TRACE_ID}-b7ad6b7169203331-01"})
        assert len(exporter.get_finished_spans()) == 1
    finally:
        tracing.shutdown_tracing()


def test_log_lines_carry_trace_and_span_ids(app_client, spans):
    stream = io.StringIO()
    configure_logging(fmt="json", stream=stream)
    try:
        app_client.get("/items/1")
    finally:
        logger.remove()
        logger.add(sys.stderr)
    rec = [json.loads(x) for x in stream.getvalue().splitlines()]
    handler = [r for r in rec if r["message"] == "in handler"][0]
    (s,) = spans.get_finished_spans()
    assert handler["trace_id"] == format(s.context.trace_id, "032x")
    assert handler["span_id"] == format(s.context.span_id, "016x")


def test_no_trace_ids_in_logs_when_tracing_is_off():
    stream = io.StringIO()
    configure_logging(fmt="json", stream=stream)
    try:
        logger.info("plain")
    finally:
        logger.remove()
        logger.add(sys.stderr)
    rec = json.loads(stream.getvalue().splitlines()[0])
    assert "trace_id" not in rec and "span_id" not in rec


async def test_concurrent_requests_do_not_share_traces(spans):
    router = make_router({"p": make_provider("p")})

    async def one(i):
        with tracing.start_span(f"req-{i}"):
            await router.route(request(f"m{i}"))

    await asyncio.gather(*(one(i) for i in range(5)))
    for i in range(5):
        (root,) = by_name(spans, f"req-{i}")
        kids = [
            s for s in spans.get_finished_spans()
            if s.context.trace_id == root.context.trace_id and s is not root
        ]
        assert {k.name for k in kids} == {"caips.route", "caips.provider_call"}
