"""Usage ledger and /v1/usage/report."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from coreai.usage import UsageLedger, UsageQueryError, to_csv
from tests.helpers import make_provider, make_router

DAY = 86400


@pytest.mark.parametrize(
    "kwargs",
    [
        {"retention_seconds": 0},
        {"retention_seconds": -1},
        {"retention_seconds": 1.5},
        {"max_rows": 0},
        {"max_rows": -1},
        {"max_rows": 1.5},
        {"max_rows": True},
    ],
)
def test_usage_ledger_rejects_invalid_capacity(kwargs):
    with pytest.raises(ValueError):
        UsageLedger(**kwargs)


class Clock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


def test_buckets_and_grouping():
    c = Clock(0)
    led = UsageLedger(clock=c)
    led.record("a", "p1", "m1", 10, 5, 0.01)
    led.record("a", "p2", "m2", 1, 1, 0.02)
    c.t = DAY + 10
    led.record("b", "p1", "m1", 3, 3, 0.03)
    rows = led.report(bucket="day")
    assert [r["period_start"] for r in rows] == [0, DAY]
    assert rows[0]["requests"] == 2 and rows[0]["cost_usd"] == 0.03
    by_p = led.report(bucket="day", group_by=["provider"])
    assert {(r["period_start"], r["provider"]) for r in by_p} == {
        (0, "p1"),
        (0, "p2"),
        (DAY, "p1"),
    }
    assert [r["subject"] for r in led.report(subject="b", group_by=["subject"])] == [
        "b"
    ]


def test_since_until_and_validation():
    c = Clock(0)
    led = UsageLedger(clock=c)
    led.record("a", cost_usd=1)
    c.t = 5 * 3600
    led.record("a", cost_usd=2)
    assert led.report(since=3600)[0]["cost_usd"] == 2
    assert led.report(until=3600)[0]["cost_usd"] == 1
    for bad in (dict(bucket="week"), dict(group_by=["x"]), dict(since=5, until=5)):
        with pytest.raises(UsageQueryError):
            led.report(**bad)


def test_retention_and_row_cap_bound_memory():
    c = Clock(0)
    led = UsageLedger(retention_seconds=2 * 3600, max_rows=3, clock=c)
    for i in range(10):
        c.t = i * 3600
        led.record(f"s{i}", cost_usd=1)
    assert len(led._rows) <= 3
    assert all(k[0] >= c.t - 2 * 3600 for k in led._rows)


def test_retention_prunes_late_rows_after_clock_moves_backwards():
    c = Clock(1000 * 3600)
    led = UsageLedger(retention_seconds=720 * 3600, clock=c)
    led.record("current")
    c.t = 1 * 3600
    led.record("late")
    c.t = 1001 * 3600
    led.record("current")
    assert all(hour >= 281 * 3600 for hour, *_ in led._rows)


def test_csv_header_and_formula_injection_guard():
    led = UsageLedger(clock=Clock(0))
    led.record("a", "p", "=cmd()", 1, 2, 0.5)
    rows = led.report(group_by=["model"])
    out = to_csv(rows, ["model"]).splitlines()
    assert out[0] == "period_start,model,requests,input_tokens,output_tokens,cost_usd"
    assert out[1].startswith("0,'=cmd()")


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-fixture")
    import api.server as server
    from api.auth import create_access_token
    from api.routes import router as api_router

    app = FastAPI()
    app.include_router(api_router)
    orch = MagicMock()
    orch.stats.return_value = {}
    ledger = UsageLedger()
    router = make_router({"p": make_provider("p", cost=0.25)})
    patches = [
        patch.object(server, "get_router", return_value=router),
        patch.object(server, "get_orchestrator", return_value=orch),
        patch.object(server, "get_usage_ledger", return_value=ledger),
    ]
    for p in patches:
        p.start()

    def client(sub, scopes):
        c = TestClient(app)
        c.headers["Authorization"] = "Bearer " + create_access_token(sub, scopes)
        return c

    yield client
    for p in patches:
        p.stop()


BODY = {"messages": [{"role": "user", "content": "hi"}]}
RW = ["completions:read", "completions:write"]


def test_report_is_per_caller_and_admin_sees_all(api):
    alice, bob = api("alice", RW), api("bob", RW)
    alice.post("/v1/completions", json=BODY)
    alice.post("/v1/completions", json=BODY)
    bob.post("/v1/completions", json=BODY)
    mine = alice.get("/v1/usage/report").json()["rows"]
    assert mine[0]["requests"] == 2 and mine[0]["cost_usd"] == 0.5
    # a non-admin asking to group by subject still sees only themselves
    g = alice.get("/v1/usage/report?group_by=subject,model").json()["rows"]
    assert all("subject" not in r and r["requests"] == 2 for r in g)
    admin = api("root", ["admin"])
    rows = admin.get("/v1/usage/report?group_by=subject").json()["rows"]
    assert {r["subject"]: r["requests"] for r in rows} == {"alice": 2, "bob": 1}


def test_report_csv_and_bad_params(api):
    c = api("alice", RW)
    c.post("/v1/completions", json=BODY)
    r = c.get("/v1/usage/report?format=csv&group_by=provider")
    assert r.headers["content-type"].startswith("text/csv")
    assert r.text.splitlines()[0].startswith("period_start,provider,requests")
    assert "p," in r.text.splitlines()[1]
    assert c.get("/v1/usage/report?format=xml").status_code == 400
    assert c.get("/v1/usage/report?bucket=year").status_code == 400
    assert c.get("/v1/usage/report?group_by=prompt").status_code == 400
