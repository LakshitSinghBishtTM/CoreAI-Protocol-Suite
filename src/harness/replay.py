"""harness/replay.py: replay a recorded run against a model, without side effects.

For every assistant turn in the stored history, the model is asked again with
the exact recorded prefix (task, earlier replies and the *recorded* tool
results). Tools are never executed, so replay is safe to run on runs that
used destructive tools. The result says, per turn, whether the model would
still make the same tool calls (names and arguments) or give a text answer.
Use it to check a new model or routing policy against real past runs.

Compacted history is replayed as stored: turns before compaction are gone.
"""

import json
from dataclasses import replace
from typing import Optional

from providers.base import CompletionRequest

from .runner import REVIEW_SPEC


def _calls(raw) -> list[tuple[str, str]]:
    out = []
    for c in raw or []:
        try:
            args = json.dumps(json.loads(c.get("arguments") or "{}"), sort_keys=True)
        except (TypeError, ValueError):
            args = str(c.get("arguments"))
        out.append((c.get("name", ""), args))
    return out


async def replay_run(
    runner, rid: str, *, model: Optional[str] = None, route=None
) -> dict:
    """Re-ask ``model`` (or the agent's own) at each recorded assistant turn."""
    run = runner.store.get(rid)
    if run is None:
        raise KeyError(rid)
    agent = runner._agent(run["agent"]).effective()
    if model:
        agent = replace(agent, model=model)
    route = route or runner.route
    msgs = runner.store.messages(rid)
    tools = runner.registry.specs(agent.allowed_tools) + (
        [REVIEW_SPEC] if agent.ask_when_uncertain else []
    )
    turns: list = []
    cost = 0.0
    for i, m in enumerate(msgs):
        if m.role != "assistant":
            continue
        req = CompletionRequest(
            messages=list(msgs[:i]), model=agent.model, max_tokens=agent.max_tokens,
            temperature=agent.temperature, system_prompt=agent.system_prompt,
            tools=tools or None,
            metadata={"run_id": rid, "agent": agent.name, "replay": True},
        )  # fmt: skip
        resp = await route(req)
        cost += resp.cost_usd or 0.0
        want = _calls(m.tool_calls)
        got = _calls((resp.metadata or {}).get("tool_calls"))
        turns.append(
            {
                "turn": len(turns) + 1,
                "recorded_calls": [n for n, _ in want],
                "replayed_calls": [n for n, _ in got],
                "same_tools": [n for n, _ in want] == [n for n, _ in got],
                "same_arguments": want == got,
                "model": resp.model,
            }
        )
    return {
        "run_id": rid,
        "model": model or agent.model,
        "turns": turns,
        "matching_turns": sum(t["same_tools"] for t in turns),
        "cost_usd": round(cost, 6),
    }
