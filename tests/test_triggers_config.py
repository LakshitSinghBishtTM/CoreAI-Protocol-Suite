import pytest

from harness.runtime import build_runtime
from providers.base import CompletionResponse
from tools import ToolRegistry


async def route(req):
    return CompletionResponse("ok", "m", "p", 1, 1, 0.0, 1.0)


CFG = {
    "db": ":memory:",
    "agents": {"rep": {"system_prompt": "s"}},
    "triggers": {
        "nightly": {"every_s": 60, "agent": "rep", "task": "go"},
        "outage": {"event": "provider.outage", "agent": "rep", "task": "$provider", "match": {"provider": "x"}},
    },
}


def test_triggers_built_from_config():
    rt = build_runtime(CFG, route, ToolRegistry())
    assert set(rt.triggers.triggers) == {"nightly", "outage"}
    assert rt.triggers.triggers["outage"].match == {"provider": "x"}


@pytest.mark.parametrize(
    "bad,msg",
    [
        ({"agent": "rep", "task": "t"}, "exactly one"),
        ({"every_s": 5, "event": "e", "agent": "rep", "task": "t"}, "exactly one"),
        ({"every_s": 5, "agent": "nobody", "task": "t"}, "nobody"),
        ({"every_s": 5, "agent": "rep", "task": "t", "bogus": 1}, "unknown fields"),
    ],
)
def test_bad_trigger_config_reports_all_problems(bad, msg):
    cfg = {**CFG, "triggers": {"t": bad}}
    with pytest.raises(ValueError, match=msg):
        build_runtime(cfg, route, ToolRegistry())


async def test_start_and_stop_manage_loop_and_bus():
    from utils.events import bus

    rt = build_runtime(CFG, route, ToolRegistry())
    before = len(bus.sinks)
    rt.start()
    assert len(bus.sinks) == before + 1 and rt._trigger_loop is not None
    await rt.stop()
    assert len(bus.sinks) == before
