"""
HTTP behaviour under provider failure: status codes, headers, no leaked
upstream text, streaming errors, and the provider health endpoint.
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.helpers import (
    StatusError,
    hang_forever,
    make_provider,
    make_router,
    make_stream_provider,
)

BODY = {"messages": [{"role": "user", "content": "hi"}]}


@pytest.fixture
def secret(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "api-reliability-test-secret")


@pytest.fixture
def auth(secret):
    from api.auth import create_access_token

    token = create_access_token(
        "user:test", ["completions:read", "completions:write", "admin"]
    )
    return {"Authorization": f"Bearer {token}"}


def client_for(router):
    """A client whose routes use *router* instead of the real server state."""
    import api.server as server
    from api.routes import router as api_router

    app = FastAPI()
    app.include_router(api_router)
    orch = MagicMock()
    orch.stats.return_value = {}
    patches = [
        patch.object(server, "get_router", return_value=router),
        patch.object(server, "get_orchestrator", return_value=orch),
    ]
    for p in patches:
        p.start()
    c = TestClient(app)
    c._patches = patches
    return c


@pytest.fixture
def make_client():
    clients = []

    def build(router):
        c = client_for(router)
        clients.append(c)
        return c

    yield build
    for c in clients:
        for p in c._patches:
            p.stop()


# ------------------------------------------------------------------ completions


def test_success(make_client, auth):
    c = make_client(make_router({"a": make_provider("a")}))
    r = c.post("/v1/completions", json=BODY, headers=auth)
    assert r.status_code == 200 and r.json()["provider"] == "a"


def test_failover_is_invisible_to_the_client(make_client, auth):
    router = make_router(
        {"a": make_provider("a", behaviour=StatusError(529)), "b": make_provider("b")},
        max_retry_attempts=1,
    )
    r = make_client(router).post("/v1/completions", json=BODY, headers=auth)
    assert r.status_code == 200 and r.json()["provider"] == "b"


def test_overloaded_provider_returns_503_not_500(make_client, auth):
    router = make_router(
        {"a": make_provider("a", behaviour=StatusError(529, "Overloaded"))},
        max_retry_attempts=1,
    )
    r = make_client(router).post("/v1/completions", json=BODY, headers=auth)
    assert r.status_code == 503


def test_rate_limited_returns_429_with_retry_after(make_client, auth):
    exc = StatusError(429, "slow down", headers={"retry-after": "7"})
    router = make_router({"a": make_provider("a", behaviour=exc)}, max_retry_attempts=1)
    r = make_client(router).post("/v1/completions", json=BODY, headers=auth)
    assert r.status_code == 429
    assert r.headers["retry-after"] == "7"


def test_provider_timeout_returns_504(make_client, auth):
    router = make_router(
        {"a": make_provider("a", behaviour=hang_forever())},
        request_timeout_seconds=0.05,
        max_retry_attempts=1,
    )
    r = make_client(router).post("/v1/completions", json=BODY, headers=auth)
    assert r.status_code == 504


def test_deadline_exceeded_returns_504(make_client, auth):
    router = make_router(
        {"a": make_provider("a", behaviour=hang_forever())},
        request_timeout_seconds=10,
        deadline_seconds=0.05,
    )
    r = make_client(router).post("/v1/completions", json=BODY, headers=auth)
    assert r.status_code == 504 and "deadline" in r.json()["detail"].lower()


def test_all_providers_down_returns_503_listing_reasons(make_client, auth):
    router = make_router(
        {
            "a": make_provider("a", behaviour=StatusError(529)),
            "b": make_provider("b", behaviour=StatusError(500)),
        },
        max_retry_attempts=1,
    )
    r = make_client(router).post("/v1/completions", json=BODY, headers=auth)
    assert r.status_code == 503
    detail = r.json()["detail"]
    assert detail["error"] == "all_providers_unavailable"
    assert detail["failures"] == [
        {"provider": "a", "reason": "overloaded"},
        {"provider": "b", "reason": "server_error"},
    ]


def test_open_circuits_return_503_with_retry_after(make_client, auth):
    router = make_router(
        {"a": make_provider("a", behaviour=StatusError(500))},
        circuit_failure_threshold=1,
        max_retry_attempts=1,
    )
    c = make_client(router)
    c.post("/v1/completions", json=BODY, headers=auth)  # opens the breaker
    r = c.post("/v1/completions", json=BODY, headers=auth)
    assert r.status_code == 503
    assert int(r.headers["retry-after"]) >= 1


def test_unknown_provider_is_a_400(make_client, auth):
    c = make_client(make_router({"a": make_provider("a")}))
    body = {**BODY, "provider": "nope"}
    r = c.post("/v1/completions", json=body, headers=auth)
    assert r.status_code == 400 and "not available" in r.json()["detail"]


def test_upstream_bad_request_is_a_400(make_client, auth):
    router = make_router(
        {"a": make_provider("a", behaviour=StatusError(400, "max_tokens too large"))}
    )
    r = make_client(router).post("/v1/completions", json=BODY, headers=auth)
    assert r.status_code == 400 and "max_tokens too large" in r.json()["detail"]


def test_provider_auth_failure_does_not_leak_key_material(make_client, auth):
    secret_text = "Incorrect API key provided: sk-live-SECRETKEYFRAGMENT1234"
    router = make_router(
        {"a": make_provider("a", behaviour=StatusError(401, secret_text))},
        max_retry_attempts=1,
    )
    r = make_client(router).post("/v1/completions", json=BODY, headers=auth)
    assert r.status_code == 502
    assert "SECRETKEYFRAGMENT" not in r.text


def test_unexpected_errors_are_500_without_internal_details(make_client, auth):
    router = make_router(
        {"a": make_provider("a", behaviour=RuntimeError("db password is hunter2"))}
    )
    r = make_client(router).post("/v1/completions", json=BODY, headers=auth)
    assert r.status_code == 500
    assert "hunter2" not in r.text


# ------------------------------------------------------------------ streaming


def test_stream_success(make_client, auth):
    router = make_router({"a": make_stream_provider("a", chunks=("Hel", "lo"))})
    r = make_client(router).post("/v1/completions/stream", json=BODY, headers=auth)
    assert r.status_code == 200 and r.text == "Hello"


def test_stream_that_cannot_open_is_an_http_error_not_a_200(make_client, auth):
    router = make_router(
        {"a": make_stream_provider("a", open_error=StatusError(529))},
        max_retry_attempts=1,
    )
    r = make_client(router).post("/v1/completions/stream", json=BODY, headers=auth)
    assert r.status_code == 503


def test_stream_fails_over_before_first_chunk(make_client, auth):
    router = make_router(
        {
            "a": make_stream_provider("a", open_error=StatusError(529)),
            "b": make_stream_provider("b", chunks=("from-b",)),
        },
        max_retry_attempts=1,
    )
    r = make_client(router).post("/v1/completions/stream", json=BODY, headers=auth)
    assert r.status_code == 200 and r.text == "from-b"


def test_mid_stream_failure_is_marked_without_leaking_the_message(make_client, auth):
    exc = StatusError(500, "internal path /srv/secret/app.py exploded")
    router = make_router(
        {"a": make_stream_provider("a", chunks=("par", "tial"), fail_after=(1, exc))},
        max_retry_attempts=1,
    )
    r = make_client(router).post("/v1/completions/stream", json=BODY, headers=auth)
    assert r.status_code == 200  # headers were already sent with the first chunk
    assert r.text.startswith("par")
    assert r.text.endswith("[stream_error] server_error")
    assert "/srv/secret" not in r.text


def test_stream_releases_the_rate_limit_slot_when_done(make_client, auth):
    router = make_router({"a": make_stream_provider("a")})
    make_client(router).post("/v1/completions/stream", json=BODY, headers=auth)
    assert router.limiter.get_limiter("a").active_requests == 0


# ------------------------------------------------------------------ providers/stats


def test_providers_endpoint_reports_real_health_without_network_calls(
    make_client, auth
):
    router = make_router({"a": make_provider("a")})
    r = make_client(router).get("/v1/providers", headers=auth)
    assert r.status_code == 200
    (entry,) = r.json()
    assert entry["name"] == "a"
    assert entry["available"] is True
    assert entry["circuit_state"] == "closed"


def test_providers_endpoint_shows_an_open_circuit_as_unavailable(make_client, auth):
    router = make_router(
        {"a": make_provider("a", behaviour=StatusError(500)), "b": make_provider("b")},
        circuit_failure_threshold=1,
        max_retry_attempts=1,
    )
    c = make_client(router)
    c.post("/v1/completions", json=BODY, headers=auth)
    entries = {e["name"]: e for e in c.get("/v1/providers", headers=auth).json()}
    assert entries["a"]["available"] is False
    assert entries["a"]["circuit_state"] == "open"
    assert entries["a"]["error_kinds"] == {"server_error": 1}
    assert entries["b"]["available"] is True


def test_stats_endpoint_includes_failovers(make_client, auth):
    router = make_router(
        {"a": make_provider("a", behaviour=StatusError(500)), "b": make_provider("b")},
        max_retry_attempts=1,
    )
    c = make_client(router)
    c.post("/v1/completions", json=BODY, headers=auth)
    r = c.get("/v1/stats", headers=auth)
    assert r.status_code == 200 and r.json()["failovers"] == 1
