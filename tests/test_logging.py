"""Structured JSON logging and request IDs."""

import io
import json
import logging
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from loguru import logger

from coreai.logging_setup import configure_logging, redact
from coreai.request_context import RequestContextMiddleware, current_request_id
from tests.helpers import make_provider, make_router, request


@pytest.fixture
def buf():
    stream = io.StringIO()
    configure_logging(fmt="json", level="DEBUG", stream=stream)
    yield stream
    logger.remove()
    logger.add(sys.stderr)


def lines(stream):
    return [json.loads(x) for x in stream.getvalue().splitlines() if x.strip()]


def test_json_line_has_standard_fields_and_extras(buf):
    logger.bind(provider="a", cost_usd=0.5).info("hello {x}")
    (rec,) = lines(buf)
    assert rec["message"] == "hello {x}"  # braces in messages are safe
    assert rec["level"] == "INFO" and rec["provider"] == "a"
    assert rec["cost_usd"] == 0.5 and rec["request_id"] is None
    assert rec["ts"].count("T") == 1 and "logger" in rec


def test_clashing_extra_names_are_prefixed(buf):
    logger.bind(level="mine", message="m").info("real")
    (rec,) = lines(buf)
    assert rec["level"] == "INFO" and rec["message"] == "real"
    assert rec["x_level"] == "mine" and rec["x_message"] == "m"


def test_secrets_are_masked(buf):
    logger.info("auth Bearer abcdefghijklmnop key sk-abcdefghijklmnopqrstuv end")
    (rec,) = lines(buf)
    assert "abcdefghijklmnop" not in rec["message"]
    assert "Bearer [redacted]" in rec["message"] and "sk-[redacted]" in rec["message"]
    assert redact("nothing here") == "nothing here"


def test_exceptions_are_recorded_without_traceback_spam(buf):
    try:
        raise ValueError("boom")
    except ValueError:
        logger.opt(exception=True).error("failed")
    (rec,) = lines(buf)
    assert rec["exception"] == "ValueError: boom"


def test_stdlib_logging_is_bridged(buf):
    logging.getLogger("some.module").warning("from stdlib %s", "ok")
    recs = [r for r in lines(buf) if "from stdlib" in r["message"]]
    assert recs and recs[0]["level"] == "WARNING" and "ok" in recs[0]["message"]


def test_invalid_format_is_rejected():
    with pytest.raises(ValueError):
        configure_logging(fmt="xml")


def test_text_format_still_carries_request_id():
    stream = io.StringIO()
    configure_logging(fmt="text", stream=stream)
    try:
        from coreai.request_context import _request_id

        token = _request_id.set("rid-12345678")
        logger.info("plain {braces}")
        _request_id.reset(token)
    finally:
        logger.remove()
        logger.add(sys.stderr)
    assert "rid-12345678" in stream.getvalue() and "{braces}" in stream.getvalue()


def test_file_rotation_and_retention_cap_disk_use(tmp_path):
    path = tmp_path / "gw.log"
    configure_logging(fmt="json", file=str(path), rotation="2 KB", retention=2)
    try:
        for i in range(400):
            logger.bind(i=i).info("x" * 50)
        logger.complete()
    finally:
        logger.remove()
        logger.add(sys.stderr)
    files = list(tmp_path.glob("gw*"))
    assert 1 < len(files) <= 3  # live file plus at most 2 kept
    for f in files:
        for line in f.read_text().splitlines():
            assert json.loads(line)["level"] == "INFO"


# ---------------------------- request IDs ----------------------------


@pytest.fixture
def app_client(buf):
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)

    @app.get("/work")
    async def work():
        logger.info("inside handler")
        return {"rid": current_request_id()}

    @app.get("/boom")
    async def boom():
        raise RuntimeError("x")

    @app.get("/healthz")
    async def hz():
        return {}

    return TestClient(app, raise_server_exceptions=False)


def test_generates_id_and_logs_it_on_every_line(app_client, buf):
    r = app_client.get("/work")
    rid = r.headers["x-request-id"]
    assert r.json()["rid"] == rid and len(rid) >= 8
    recs = [x for x in lines(buf) if x["message"] in ("inside handler", "request")]
    assert len(recs) == 2
    assert {x["request_id"] for x in recs} == {rid}
    access = [x for x in recs if x["message"] == "request"][0]
    assert access["path"] == "/work" and access["status"] == 200
    assert access["method"] == "GET" and access["duration_ms"] >= 0


def test_safe_client_id_is_kept_unsafe_is_replaced(app_client):
    ok = app_client.get("/work", headers={"X-Request-ID": "client-id-12345"})
    assert ok.headers["x-request-id"] == "client-id-12345"
    bad = app_client.get("/work", headers={"X-Request-ID": "bad id\twith spaces!"})
    assert bad.headers["x-request-id"] != "bad id\twith spaces!"
    short = app_client.get("/work", headers={"X-Request-ID": "a"})
    assert short.headers["x-request-id"] != "a"


def test_ids_do_not_leak_between_requests(app_client, buf):
    a = app_client.get("/work").headers["x-request-id"]
    b = app_client.get("/work").headers["x-request-id"]
    assert a != b
    assert current_request_id() is None


def test_server_errors_are_logged_as_warnings_with_500(app_client, buf):
    r = app_client.get("/boom")
    assert r.status_code == 500
    access = [x for x in lines(buf) if x["message"] == "request"][-1]
    assert access["status"] == 500 and access["level"] == "WARNING"


def test_probe_paths_are_not_access_logged(app_client, buf):
    app_client.get("/healthz")
    assert not [x for x in lines(buf) if x["message"] == "request"]


def test_real_server_adds_request_id_even_when_draining(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "log-test-secret")
    import api.server as server

    server.drain.begin_drain()
    try:
        r = TestClient(server.app).get("/v1/providers")
    finally:
        server.drain.resume()
    assert r.status_code == 503 and r.headers.get("x-request-id")


# ------------------------ provider decision logging ------------------------


async def test_provider_response_line_explains_the_decision(buf):
    from tests.helpers import StatusError

    router = make_router(
        {
            "bad": make_provider("bad", behaviour=StatusError(500)),
            "good": make_provider("good"),
        }
    )
    await router.route(request())
    rec = [x for x in lines(buf) if x["message"] == "provider response"][0]
    assert rec["provider"] == "good" and rec["model"] == "good-model"
    assert rec["input_tokens"] == 20 and rec["output_tokens"] == 10
    assert rec["failed_over_from"] == ["bad"] and rec["hedged"] is False
    assert "cost_usd" in rec and "latency_ms" in rec
