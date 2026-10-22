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

    assert _client(h).chat("fast", [{"role": "user", "content": "hi"}]) == {"choices": []}
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
    c = _client(lambda r: httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -1}}))
    with pytest.raises(CaipsError):
        c.call_tool("x")


def test_decide_sends_body():
    got = {}

    def h(req):
        got["body"] = req.content
        return httpx.Response(200, json={"ok": True})

    _client(h).decide("a1", True)
    assert b'"approve":true' in got["body"].replace(b" ", b"")
