"""Prometheus rendering, /healthz, /readyz and /metrics."""

import pytest
from fastapi.testclient import TestClient

from coreai.metrics import render_prometheus
from tests.helpers import make_provider, make_router

STATS = {
    "total_requests": 7,
    "failovers": 2,
    "hedges": {"launched": 1, "won_by_second": 1},
    "provider_stats": {
        'a"x': {
            "requests": 5,
            "errors": 1,
            "total_cost": 0.25,
            "p50_latency_ms": 120.0,
            "health": 0.9,
            "error_kinds": {"server_error": 1},
            "circuit": {"state": "open", "times_opened": 3},
        },
        "b": {
            "requests": 2,
            "errors": 0,
            "total_cost": 0,
            "p50_latency_ms": None,
            "health": 1.0,
            "error_kinds": {},
            "circuit": {"state": "closed", "times_opened": 0},
        },
    },
    "cache_stats": {"hits": 4, "misses": 6},
}


def test_render_counters_and_labels():
    text = render_prometheus(STATS, accepting=False, in_flight=2)
    assert "caips_requests_total 7" in text
    assert 'caips_provider_requests_total{provider="b"} 2' in text
    assert 'caips_provider_errors_total{provider="a\\"x",kind="server_error"} 1' in text
    assert 'caips_circuit_state{provider="a\\"x",state="open"} 1' in text
    assert 'caips_circuit_state{provider="a\\"x",state="closed"} 0' in text
    assert "caips_cache_hits_total 4" in text
    assert "caips_accepting_traffic 0" in text
    assert "caips_in_flight_requests 2" in text
    assert text.endswith("\n")


def test_missing_latency_is_omitted_not_zero():
    text = render_prometheus(STATS)
    assert 'caips_provider_latency_p50_ms{provider="b"}' not in text


def test_every_metric_has_help_and_type():
    lines = render_prometheus(STATS).splitlines()
    names = {ln.split("{")[0].split(" ")[0] for ln in lines if not ln.startswith("#")}
    for n in names:
        assert f"# HELP {n} " in "\n".join(lines)
        assert f"# TYPE {n} " in "\n".join(lines)


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "metrics-test-secret")
    import api.server as server

    old = server.app_state["router"]
    server.app_state["router"] = make_router({"p": make_provider("p")})
    server.drain.resume()
    yield server
    server.app_state["router"] = old
    server.drain.resume()


def test_healthz_and_readyz(server):
    c = TestClient(server.app)
    assert c.get("/healthz").json() == {"status": "ok"}
    r = c.get("/readyz")
    assert r.status_code == 200 and r.json()["status"] == "ready"


def test_readyz_503_while_draining_but_healthz_stays_up(server):
    c = TestClient(server.app)
    server.drain.begin_drain()
    assert c.get("/healthz").status_code == 200
    r = c.get("/readyz")
    assert r.status_code == 503 and "draining" in r.json()["reasons"]


def test_readyz_503_when_all_circuits_open(server):
    router = server.app_state["router"]
    for b in router.breakers.values():
        for _ in range(50):
            b.record_failure()
    r = TestClient(server.app).get("/readyz")
    assert r.status_code == 503


def test_metrics_requires_admin(server):
    from api.auth import create_access_token

    c = TestClient(server.app)
    assert c.get("/metrics").status_code in (401, 403)
    token = create_access_token("user:t", ["completions:read"])
    r = c.get("/metrics", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403
    token = create_access_token("user:t", ["admin"])
    r = c.get("/metrics", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert "caips_requests_total" in r.text
    assert r.headers["content-type"].startswith("text/plain")


# ------------------- latency histogram and token counters -------------------

import re  # noqa: E402

from tests.helpers import request, response  # noqa: E402


def parse(text, metric):
    """{frozenset(label items): value} for samples of *metric*."""
    out = {}
    for line in text.splitlines():
        m = re.match(rf"^{metric}(?:\{{(.*)\}})? (\S+)$", line)
        if m:
            labels = dict(re.findall(r'(\w+)="((?:[^"\\]|\\.)*)"', m.group(1) or ""))
            out[frozenset(labels.items())] = float(m.group(2))
    return out


async def served(latencies, model="m1"):
    outcomes = []
    for ms in latencies:
        r = response("p", latency_ms=ms)
        r.model = model
        outcomes.append(r)
    router = make_router({"p": make_provider("p", behaviour=outcomes)})
    for i in range(len(latencies)):
        await router.route(request(f"hi {i}"))
    return router


async def test_histogram_is_cumulative_and_consistent():
    router = await served([40, 100, 300, 99999])
    text = render_prometheus(router.stats())
    base = {("provider", "p"), ("model", "m1")}
    buckets = parse(text, "caips_request_duration_seconds_bucket")

    def at(le):
        return buckets[frozenset(base | {("le", le)})]

    assert at("0.05") == 1  # 40ms
    assert at("0.1") == 2  # 40, 100 (bound is inclusive)
    assert at("0.25") == 2
    assert at("0.5") == 3
    assert at("30") == 3
    assert at("+Inf") == 4
    count = parse(text, "caips_request_duration_seconds_count")
    total = parse(text, "caips_request_duration_seconds_sum")
    assert count[frozenset(base)] == 4
    assert total[frozenset(base)] == pytest.approx((40 + 100 + 300 + 99999) / 1000)
    values = [buckets[k] for k in buckets]
    assert max(values) == 4


async def test_token_counters_by_model_and_direction():
    router = await served([100, 100])
    tokens = parse(render_prometheus(router.stats()), "caips_tokens_total")
    key = {("provider", "p"), ("model", "m1")}
    assert tokens[frozenset(key | {("type", "input")})] == 40  # 2 x 20
    assert tokens[frozenset(key | {("type", "output")})] == 20  # 2 x 10


async def test_model_labels_are_bounded(monkeypatch):
    import coreai.router as r

    monkeypatch.setattr(r, "_MAX_MODELS_PER_PROVIDER", 3)
    router = make_router({"p": make_provider("p")})
    for i in range(10):
        router._record_usage("p", f"model-{i}", 100.0, 1, 1)
    models = {m for (_, m) in router.model_stats}
    assert len(models) == 4 and "other" in models  # 3 named + shared bucket
    assert sum(e["requests"] for e in router.model_stats.values()) == 10


def test_no_histogram_without_traffic():
    router = make_router({"p": make_provider("p")})
    text = render_prometheus(router.stats())
    assert "caips_request_duration_seconds" not in text
