"""harness/workflow.py: fixed-step workflows for work a model must not steer.

A workflow is an ordered list of steps defined in config. The order is fixed:
no model decides what runs next. Step kinds:

* ``tool``:  call a registered tool with templated arguments.
* ``model``: one model call (no tools) with a templated prompt.

Templates use ``${input}`` and ``${<step id>}`` (the output of an earlier
step). Tool output reaches a model prompt only as labeled untrusted data.
Tool calls go through the registry, so allow lists, schema validation, rate
limits and approvals all apply; a step that needs approval stops the workflow
with ``status="needs_approval"`` and a later call can name it in ``approved``.
"""

import json
import string
from dataclasses import dataclass, field
from typing import Optional

from providers.base import CompletionRequest, Message
from tools.registry import ApprovalRequired, Permission, ToolError


@dataclass
class Step:
    id: str
    kind: str  # "tool" | "model"
    tool: str = ""
    args: dict = field(default_factory=dict)
    prompt: str = ""
    model: Optional[str] = None


def parse_steps(raw: list[dict]) -> list[Step]:
    steps, seen, problems = [], set(), []
    for i, d in enumerate(raw or []):
        sid = d.get("id", "")
        if not sid or sid in seen or sid == "input":
            problems.append(f"step {i}: id missing, duplicate or reserved")
        seen.add(sid)
        kind = "tool" if "tool" in d else "model" if "prompt" in d else ""
        if not kind:
            problems.append(f"step {sid or i}: needs 'tool' or 'prompt'")
            continue
        steps.append(
            Step(sid, kind, d.get("tool", ""), d.get("args") or {}, d.get("prompt", ""), d.get("model"))
        )  # fmt: skip
    if not steps:
        problems.append("workflow has no steps")
    if problems:
        raise ValueError("; ".join(problems))
    return steps


def _fill(template, values: dict):
    if isinstance(template, str):
        return string.Template(template).safe_substitute(values)
    if isinstance(template, dict):
        return {k: _fill(v, values) for k, v in template.items()}
    if isinstance(template, list):
        return [_fill(v, values) for v in template]
    return template


async def run_workflow(
    steps: list[Step],
    registry,
    route,
    *,
    input: str = "",
    allowed: Optional[list[str]] = None,
    caller: str = "workflow",
    approved: Optional[set] = None,
    max_permission: Permission = Permission.DESTRUCTIVE,
    max_cost_usd: Optional[float] = None,
) -> dict:
    approved = approved or set()
    raw = {"input": input}  # what templates for tools see
    safe = {"input": input}  # what model prompts see
    out = {"status": "completed", "steps": [], "failed_step": None, "cost_usd": 0.0}
    for st in steps:
        if max_cost_usd is not None and out["cost_usd"] >= max_cost_usd:
            out.update(status="cost_limit", failed_step=st.id)
            return out
        rec = {"id": st.id, "kind": st.kind, "ok": True}
        try:
            if st.kind == "tool":
                args = _fill(st.args, raw)
                res = await registry.call(
                    st.tool, args, allowed=allowed, caller=caller,
                    approved=st.id in approved, max_permission=max_permission,
                )  # fmt: skip
                raw[st.id], safe[st.id] = res.content, res.as_untrusted()
            else:
                req = CompletionRequest(
                    messages=[Message("user", _fill(st.prompt, safe))],
                    model=st.model, max_tokens=1024, temperature=0.0,
                    metadata={"workflow_step": st.id},
                )  # fmt: skip
                resp = await route(req)
                out["cost_usd"] += resp.cost_usd or 0.0
                raw[st.id] = safe[st.id] = resp.content or ""
        except ApprovalRequired:
            rec.update(ok=False, error="approval_required")
            out["steps"].append(rec)
            out.update(status="needs_approval", failed_step=st.id)
            return out
        except ToolError as exc:
            rec.update(ok=False, error=exc.code)
            out["steps"].append(rec)
            out.update(status="failed", failed_step=st.id)
            return out
        except Exception as exc:  # noqa: BLE001  model/gateway failure
            rec.update(ok=False, error=type(exc).__name__)
            out["steps"].append(rec)
            out.update(status="failed", failed_step=st.id)
            return out
        rec["output"] = raw[st.id][:2000]
        out["steps"].append(rec)
    out["result"] = raw[steps[-1].id]
    out["cost_usd"] = round(out["cost_usd"], 6)
    return out
