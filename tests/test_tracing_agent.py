"""Tracing for agent runs: run, model call, plan and tool spans, no content."""

import pytest

pytest.importorskip("opentelemetry.sdk")
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)
from opentelemetry.trace import StatusCode  # noqa: E402

from coreai import tracing  # noqa: E402
from tests.test_harness import Script, call, make, resp  # noqa: E402

SECRET = "TOP-SECRET-TASK-TEXT"


@pytest.fixture
def spans():
    exporter = InMemorySpanExporter()
    assert tracing.setup_tracing(exporter=exporter, simple=True)
    yield exporter
    tracing.shutdown_tracing()


def named(exporter, name):
    return [s for s in exporter.get_finished_spans() if s.name == name]


async def test_run_model_call_and_tool_spans_nest_under_the_run(spans):
    r, _ = make(Script(resp(calls=[call("echo", x="1")]), resp("done")))
    out = await r.run("a", SECRET)
    (run,) = named(spans, "caips.agent.run")
    models, tools = named(spans, "caips.agent.model_call"), named(spans, "caips.tool")
    assert len(models) == 2 and len(tools) == 1
    for child in models + tools:
        assert child.parent.span_id == run.context.span_id
        assert child.context.trace_id == run.context.trace_id
    a = run.attributes
    assert a["caips.run_id"] == out["id"] and a["caips.agent"] == "a"
    assert a["caips.run.status"] == "completed" and a["caips.run.steps"] == 2
    assert a["caips.run.cost_usd"] == pytest.approx(0.02)
    assert sorted(m.attributes["caips.step"] for m in models) == [1, 2]
    assert tools[0].attributes["caips.tool"] == "echo"


async def test_plan_gets_its_own_span(spans):
    r, _ = make(Script(resp("1. go"), resp("done")), plan=True)
    await r.run("a", "t")
    assert len(named(spans, "caips.agent.plan")) == 1
    assert len(named(spans, "caips.agent.model_call")) == 1


async def test_limit_stop_is_recorded_on_the_run_span(spans):
    r, _ = make(Script(resp(calls=[call("echo", x="1")])), max_steps=1)
    await r.run("a", "t")
    (run,) = named(spans, "caips.agent.run")
    assert run.attributes["caips.run.stop_reason"] == "max_steps"
    assert run.status.status_code != StatusCode.ERROR


async def test_model_failure_marks_the_run_span_failed(spans):
    r, _ = make(Script(RuntimeError("provider said " + SECRET)))
    await r.run("a", "t")
    (run,) = named(spans, "caips.agent.run")
    assert run.status.status_code == StatusCode.ERROR
    (m,) = named(spans, "caips.agent.model_call")
    assert m.status.status_code == StatusCode.ERROR


async def test_resume_after_approval_makes_a_second_run_span(spans):
    r, _ = make(
        Script(resp(calls=[call("wipe")]), resp("done")), approval_tools=["wipe"]
    )
    out = await r.run("a", "t")
    assert out["status"] == "waiting_approval"
    await r.resume(out["id"], approve=True)
    runs = named(spans, "caips.agent.run")
    assert [s.attributes["caips.run.status"] for s in runs] == [
        "waiting_approval",
        "completed",
    ]


async def test_task_text_and_tool_arguments_never_reach_spans(spans):
    r, _ = make(Script(resp(calls=[call("echo", x=SECRET)]), resp("done " + SECRET)))
    await r.run("a", SECRET)
    text = " ".join(
        [s.name for s in spans.get_finished_spans()]
        + [str(v) for s in spans.get_finished_spans() for v in s.attributes.values()]
        + [
            str(v)
            for s in spans.get_finished_spans()
            for e in s.events
            for v in (e.attributes or {}).values()
        ]
        + [str(s.status.description) for s in spans.get_finished_spans()]
    )
    assert SECRET not in text


async def test_off_by_default_changes_nothing():
    assert not tracing.enabled()
    r, _ = make(Script(resp("done")))
    out = await r.run("a", "t")
    assert out["status"] == "completed"
