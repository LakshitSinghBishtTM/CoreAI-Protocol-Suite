"""Audit log: storage bounds, redaction, rotation, policy hook, admin endpoint."""

import json
import os

import pytest
from fastapi.testclient import TestClient

from coreai.audit import AuditLog
from coreai.policy import PolicyStore
from tests.helpers import FakeClock


def test_record_and_query_newest_first():
    log = AuditLog(clock=FakeClock(10.0))
    log.record("policy.reload", "system", target="f")
    log.record("admin.shutdown", "user:a", details={"mode": "graceful"})
    events = log.events()
    assert [e["action"] for e in events] == ["admin.shutdown", "policy.reload"]
    assert events[0]["actor"] == "user:a" and events[0]["ts"] == 10.0
    assert [e["id"] for e in events] == [2, 1]


def test_filters_by_action_prefix_and_actor():
    log = AuditLog()
    log.record("policy.reload", "system")
    log.record("policy.loaded", "system")
    log.record("admin.shutdown", "user:a")
    assert len(log.events(action="policy")) == 2
    assert len(log.events(action="policy.reload")) == 1
    assert len(log.events(action="pol")) == 0  # prefix is per segment
    assert len(log.events(actor="user:a")) == 1
    assert len(log.events(limit=1)) == 1


def test_ring_is_bounded():
    log = AuditLog(max_events=5)
    for i in range(50):
        log.record("x", "u", details={"i": i})
    events = log.events(limit=1000)
    assert len(events) == 5 and events[0]["details"]["i"] == 49


def test_secrets_and_long_values_are_redacted_or_cut():
    log = AuditLog()
    e = log.record(
        "key.created",
        "admin",
        details={"api_key": "sk-123", "nested": {"Token": "t"}, "note": "x" * 2000},
    )
    assert e["details"]["api_key"] == "[redacted]"
    assert e["details"]["nested"]["Token"] == "[redacted]"
    assert len(e["details"]["note"]) < 600
    assert "sk-123" not in json.dumps(log.events())


def test_file_output_and_rotation(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path=str(path), max_bytes=300)
    for i in range(20):
        log.record("a", "u", details={"i": i})
    assert os.path.exists(str(path) + ".1")  # rotated
    assert os.path.getsize(path) < 300 + 200  # never far past the cap
    lines = path.read_text().splitlines()
    assert all(json.loads(line)["action"] == "a" for line in lines)


def test_unwritable_file_does_not_raise(tmp_path):
    log = AuditLog(path=str(tmp_path / "missing_dir" / "a.jsonl"))
    log.record("a", "u")
    assert log.write_errors == 1 and len(log.events()) == 1


GOOD = "policies:\n  default:\n    strategy: balanced\n"
BAD = "policies:\n  default:\n    strategy: nonsense\n"


def test_policy_reload_outcomes_are_reported(tmp_path):
    f = tmp_path / "routing.yml"
    f.write_text(GOOD)
    clock = FakeClock()
    store = PolicyStore(str(f), clock=clock)
    seen = []
    store.on_event = lambda outcome, details: seen.append(outcome)

    f.write_text(BAD)
    os.utime(f, (1, os.stat(f).st_mtime + 10))
    clock.advance(3)
    store.current()
    f.write_text(GOOD)
    os.utime(f, (1, os.stat(f).st_mtime + 20))
    clock.advance(3)
    store.current()
    assert seen == ["rejected", "ok"]


def test_failing_audit_callback_does_not_break_reload(tmp_path):
    f = tmp_path / "routing.yml"
    f.write_text(GOOD)
    clock = FakeClock()
    store = PolicyStore(str(f), clock=clock)

    def boom(outcome, details):
        raise RuntimeError("audit down")

    store.on_event = boom
    f.write_text(
        "policies:\n  default:\n    strategy: cost_aware\n"
        "    weights: {cost: 0.7, latency: 0.3}\n"
    )
    os.utime(f, (1, os.stat(f).st_mtime + 10))
    clock.advance(3)
    before = store.reloads
    store.current()
    assert store.reloads == before + 1 and store.last_error is None


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "audit-test-secret")
    import api.server as server

    old = server.app_state.get("audit")
    server.app_state["audit"] = AuditLog()
    yield server
    server.app_state["audit"] = old


def token(*scopes):
    from api.auth import create_access_token

    return {"Authorization": f"Bearer {create_access_token('user:t', list(scopes))}"}


def test_audit_endpoint_is_admin_only_and_filters(server):
    server.get_audit().record("policy.reload", "system")
    server.get_audit().record("admin.shutdown", "user:a")
    c = TestClient(server.app)
    assert c.get("/v1/admin/audit").status_code in (401, 403)
    assert (
        c.get("/v1/admin/audit", headers=token("completions:read")).status_code == 403
    )
    r = c.get("/v1/admin/audit?action=admin", headers=token("admin"))
    assert r.status_code == 200
    assert [e["action"] for e in r.json()["events"]] == ["admin.shutdown"]
