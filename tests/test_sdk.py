import httpx
import pytest

from caips_sdk import CaipsClient, CaipsError


def _client(handler):
    return CaipsClient("http://t", api_key="k", transport=httpx.MockTransport(handler))


def test_auth_header_and_chat():
    seen = {}

    def h(req):
        seen["auth"] = req.headers["authorization"]
        seen["path"] = req.url.path
        return httpx.Response(200, json={"choices": []})

    assert _client(h).chat("fast", [{"role": "user", "content": "hi"}]) == {
        "choices": []
    }
    assert seen == {"auth": "Bearer k", "path": "/v1/chat/completions"}


def test_error_raises_with_detail():
    c = _client(lambda r: httpx.Response(403, json={"detail": "nope"}))
    with pytest.raises(CaipsError) as e:
        c.list_tools()
    assert e.value.status == 403 and e.value.detail == "nope"


def test_wait_run_polls_until_terminal():
    states = iter(["running", "running", "completed"])
    c = _client(lambda r: httpx.Response(200, json={"status": next(states)}))
    assert c.wait_run("r1", poll=0)["status"] == "completed"


def test_wait_run_timeout():
    c = _client(lambda r: httpx.Response(200, json={"status": "running"}))
    with pytest.raises(TimeoutError):
        c.wait_run("r1", timeout=0, poll=0)


def test_call_tool_surfaces_jsonrpc_error():
    c = _client(
        lambda r: httpx.Response(
            200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -1}}
        )
    )
    with pytest.raises(CaipsError):
        c.call_tool("x")


def test_decide_sends_body():
    got = {}

    def h(req):
        got["body"] = req.content
        return httpx.Response(200, json={"ok": True})

    _client(h).decide("a1", True)
    assert b'"approve":true' in got["body"].replace(b" ", b"")


# ------------------------------------------------------------ typed client


def test_new_run_methods_hit_the_right_endpoints():
    import json

    seen = []

    def h(req):
        seen.append((req.method, req.url.path, req.url.query.decode(), req.content))
        if req.url.path.endswith("/events") or req.url.path in (
            "/v1/runs",
            "/v1/harness/agents",
        ):
            return httpx.Response(200, json={"object": "list", "data": [{"seq": 1}]})
        return httpx.Response(200, json={"ok": True})

    c = _client(h)
    assert c.list_agents() == [{"seq": 1}] and c.list_runs() == [{"seq": 1}]
    assert c.run_events("r1", after=4) == [{"seq": 1}]
    c.pause_run("r1")
    c.resume_run("r1", approve=True)
    c.kill_runs("alice")
    assert [s[:3] for s in seen] == [
        ("GET", "/v1/harness/agents", ""),
        ("GET", "/v1/runs", ""),
        ("GET", "/v1/runs/r1/events", "after=4"),
        ("POST", "/v1/runs/r1/pause", ""),
        ("POST", "/v1/runs/r1/resume", ""),
        ("POST", "/v1/runs/kill", ""),
    ]
    assert json.loads(seen[4][3]) == {"approve": True}
    assert json.loads(seen[5][3]) == {"owner": "alice"}


def test_run_agent_starts_then_waits():
    calls = []

    def h(req):
        calls.append((req.method, req.url.path))
        if req.method == "POST":
            return httpx.Response(202, json={"id": "r9", "status": "running"})
        return httpx.Response(
            200, json={"id": "r9", "status": "completed", "result": "ok"}
        )

    run = _client(h).run_agent("helper", "task", poll=0)
    assert run["result"] == "ok"
    assert calls == [("POST", "/v1/runs"), ("GET", "/v1/runs/r9")]


def test_wait_run_can_stop_when_blocked_on_an_approval():
    c = _client(lambda r: httpx.Response(200, json={"status": "waiting_approval"}))
    assert (
        c.wait_run("r1", poll=0, stop_when_blocked=True)["status"] == "waiting_approval"
    )
    with pytest.raises(TimeoutError):  # default still waits
        c.wait_run("r1", timeout=0.05, poll=0.01)


def test_run_type_lists_every_column_of_the_runs_table():
    """Catches drift: a new column in the store must be added to the type."""
    import re

    from caips_sdk import types
    from harness.store import SCHEMA

    body = re.search(r"CREATE TABLE IF NOT EXISTS runs\((.*?)\);", SCHEMA, re.S).group(
        1
    )
    cols = {
        part.split()[0] for part in re.split(r",\s*(?![^()]*\))", body) if part.strip()
    } - {
        "messages"
    }  # messages are stored but never returned
    assert cols == set(types.Run.__annotations__)


def test_approval_type_matches_public_fields():
    from caips_sdk import types
    from tools.approvals import Approval

    a = Approval.__new__(Approval)
    for k, v in dict(
        id="i", caller="c", tool="t", arguments={}, status="pending", decided_by=None
    ).items():
        setattr(a, k, v)
    assert set(a.public()) == set(types.Approval.__annotations__)
