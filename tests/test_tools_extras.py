"""Phase 6: tool spans, approval webhook, replay harness."""

import json

import httpx
import pytest

pytest.importorskip("opentelemetry.sdk")
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)  # noqa: E402

from coreai import tracing  # noqa: E402
from tools import Tool, ToolError, ToolRegistry  # noqa: E402
from tools.approvals import ApprovalStore  # noqa: E402
from tools.builtin import register_builtins  # noqa: E402
from tools.replay import Recorder, replay  # noqa: E402
from tools.webhook import ApprovalWebhook, sign  # noqa: E402


@pytest.fixture
def spans():
    exporter = InMemorySpanExporter()
    assert tracing.setup_tracing(exporter=exporter, simple=True)
    yield exporter
    tracing.shutdown_tracing()


def calc():
    r = ToolRegistry()
    register_builtins(r)
    return r


async def test_tool_spans_record_outcome_but_never_values(spans):
    r = calc()
    await r.call("calculator", {"expression": "1+1"}, allowed=["*"], caller="alice")
    with pytest.raises(ToolError):
        await r.call(
            "calculator", {"expression": "SECRETVALUE"}, allowed=["*"], caller="alice"
        )
    ok, bad = spans.get_finished_spans()
    assert ok.name == "caips.tool" and ok.attributes["caips.tool"] == "calculator"
    assert (
        ok.attributes["caips.tool.outcome"] == "ok"
        and ok.attributes["caips.tool.caller"] == "alice"
    )
    assert bad.attributes["caips.tool.outcome"] == "invalid_arguments"
    dump = json.dumps(
        [
            dict(s.attributes) | {"n": s.name, "d": s.status.description}
            for s in (ok, bad)
        ]
    )
    assert "SECRETVALUE" not in dump and "1+1" not in dump


async def test_webhook_is_signed_and_hides_argument_values_by_default():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200)

    hook = ApprovalWebhook(
        "http://h/x", "s3cret", transport=httpx.MockTransport(handler)
    )
    store = ApprovalStore(on_request=hook)
    a = store.request("alice", "wipe", {"path": "/very/secret"})
    for t in list(hook.tasks):
        await t
    (req,) = seen
    body = req.content.decode()
    assert req.headers["X-CaiPS-Signature"] == sign(
        "s3cret", req.headers["X-CaiPS-Timestamp"], body
    )
    data = json.loads(body)
    assert (
        data["id"] == a.id
        and data["tool"] == "wipe"
        and data["argument_names"] == ["path"]
    )
    assert "/very/secret" not in body
    with_args = ApprovalWebhook("http://h/x", "k", include_arguments=True,
                                transport=httpx.MockTransport(handler))  # fmt: skip
    assert await with_args.send(a)
    assert json.loads(seen[-1].content)["arguments"] == {"path": "/very/secret"}


async def test_webhook_failure_never_breaks_approvals():
    hook = ApprovalWebhook(
        "http://h/x", "k", transport=httpx.MockTransport(lambda r: httpx.Response(500))
    )
    store = ApprovalStore(on_request=hook)
    a = store.request("alice", "wipe", {})
    for t in list(hook.tasks):
        await t
    assert store.get(a.id) == a  # still there and decidable (store returns copies)
    store.decide(a.id, True, "root")
    with pytest.raises(ValueError):
        ApprovalWebhook("http://h", "")  # secretless webhooks are refused


async def test_record_and_replay_catches_regressions(tmp_path):
    path = tmp_path / "calls.jsonl"
    r = calc()
    r.on_call = Recorder(path)
    await r.call("calculator", {"expression": "2+2"}, allowed=["*"])
    with pytest.raises(ToolError):
        await r.call("calculator", {"expression": "nope("}, allowed=["*"])
    assert await replay(calc(), path) == []  # same behaviour: clean

    changed = ToolRegistry()
    changed.register(Tool("calculator", "c", {"type": "object"}, lambda **kw: "5"))
    problems = await replay(changed, path)
    assert [p["line"] for p in problems] == [1, 2]
    assert (
        problems[0]["expected"]["output"] == "4" and problems[0]["got"]["output"] == "5"
    )
