"""harness/subagent.py: expose an agent as a tool, so a parent can delegate.

The child is an ordinary run of another ``AgentDef``: it has its own step,
token, cost and deadline limits, its own trace, and only the tools its own
definition allows. The parent sees one tool call and one text result.
Nesting is capped (``MAX_DEPTH``) so delegation cannot recurse without bound.
A child that needs a human decision is left waiting and the parent is told
its run id; the parent never answers approvals on a human's behalf.
"""

import contextvars

from tools.registry import Permission, Tool

MAX_DEPTH = 2
_depth: contextvars.ContextVar[int] = contextvars.ContextVar(
    "subagent_depth", default=0
)


def subagent_tool(
    runner,
    agent_name: str,
    *,
    tool_name: str = "",
    description: str = "",
    owner: str = "local",
    timeout_s: float = 0.0,
) -> Tool:
    agent = runner._agent(agent_name)  # fail at registration, not at call time
    name = tool_name or f"agent_{agent_name}"

    async def handler(task: str) -> str:
        if _depth.get() >= MAX_DEPTH:
            return f"Refused: sub-agent nesting limit ({MAX_DEPTH}) reached."
        token = _depth.set(_depth.get() + 1)
        try:
            out = await runner.run(agent_name, task, owner=owner)
        finally:
            _depth.reset(token)
        status = out["status"]
        if status == "completed":
            return out.get("result") or ""
        if status == "waiting_approval":
            return (
                f"Sub-agent paused for human approval (run {out['id']}); not completed."
            )
        return f"Sub-agent stopped: {status} ({out.get('stop_reason') or 'no reason'}); run {out['id']}."

    return Tool(
        name,
        description
        or f"Delegate a task to the '{agent_name}' agent and get its answer.",
        {
            "type": "object",
            "properties": {"task": {"type": "string"}},
            "required": ["task"],
        },
        handler,
        owner=f"agent:{agent_name}",
        timeout_s=timeout_s or (agent.deadline_s or 300.0),
        permission=Permission.WRITE,  # a child may use write tools of its own
    )
