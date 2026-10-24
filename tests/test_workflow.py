import pytest

from harness import parse_steps, run_workflow
from providers.base import CompletionResponse
from tools import Permission, Tool, ToolRegistry


def reg():
    r = ToolRegistry()
    r.register(Tool("fetch", "f", {"type": "object", "properties": {"q": {"type": "string"}}},
                    lambda q="": f"data for {q}"))  # fmt: skip
    r.register(Tool("wipe", "w", {"type": "object", "properties": {}}, lambda: "wiped",
                    permission=Permission.DESTRUCTIVE))  # fmt: skip
    return r


class Route:
    def __init__(self):
        self.prompts = []

    async def __call__(self, req):
        self.prompts.append(req.messages[0].content)
        return CompletionResponse("summary", "m", "p", 1, 1, 0.5, 1.0)


STEPS = [
    {"id": "get", "tool": "fetch", "args": {"q": "${input}"}},
    {"id": "sum", "prompt": "Summarize: ${get}"},
]


async def test_fixed_order_and_untrusted_output_in_prompt():
    route = Route()
    out = await run_workflow(parse_steps(STEPS), reg(), route, input="cats", allowed=["fetch"])
    assert out["status"] == "completed" and out["result"] == "summary"
    assert [s["id"] for s in out["steps"]] == ["get", "sum"]
    assert "data for cats" in route.prompts[0]
    assert "untrusted" in route.prompts[0].lower()  # labeled, not raw
    assert out["cost_usd"] == 0.5


async def test_tool_allow_list_enforced():
    out = await run_workflow(parse_steps(STEPS), reg(), Route(), input="x", allowed=[])
    assert out["status"] == "failed" and out["failed_step"] == "get"


async def test_destructive_step_needs_approval_then_runs():
    steps = parse_steps([{"id": "w", "tool": "wipe"}])
    out = await run_workflow(steps, reg(), Route(), allowed=["wipe"])
    assert out["status"] == "needs_approval" and out["failed_step"] == "w"
    out = await run_workflow(steps, reg(), Route(), allowed=["wipe"], approved={"w"})
    assert out["status"] == "completed" and out["result"] == "wiped"


async def test_cost_limit_stops_before_next_step():
    steps = parse_steps([{"id": "a", "prompt": "x"}, {"id": "b", "prompt": "y"}])
    out = await run_workflow(steps, reg(), Route(), max_cost_usd=0.4)
    assert out["status"] == "cost_limit" and out["failed_step"] == "b"


@pytest.mark.parametrize("bad", [[], [{"id": "a"}], [{"id": "a", "prompt": "x"}, {"id": "a", "prompt": "y"}], [{"id": "input", "prompt": "x"}]])
def test_bad_definitions_rejected(bad):
    with pytest.raises(ValueError):
        parse_steps(bad)
