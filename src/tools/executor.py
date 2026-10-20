"""tools/executor.py

Gateway-side tool execution: when a model returns tool calls, run them through
the registry and continue the conversation until the model answers in text, a
step limit is hit, or a call needs approval.

``route`` is any async callable ``CompletionRequest -> CompletionResponse``
(normally ``router.route``), so this layer never imports the router.
"""

import json
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable, Iterable, Optional

from providers.base import CompletionRequest, CompletionResponse, Message

from .registry import ApprovalRequired, Permission, ToolError, ToolRegistry

DEFAULT_MAX_STEPS = 5


@dataclass
class ToolStep:
    call_id: str
    tool: str
    arguments: dict
    ok: bool
    output: str  # what the model was shown (error text on failure)
    duration_ms: float = 0.0
    error_code: Optional[str] = None


@dataclass
class ToolLoopResult:
    response: CompletionResponse
    steps: list[ToolStep] = field(default_factory=list)
    # "answered" | "max_steps" | "needs_approval" | "cost_limit"
    stop_reason: str = "answered"
    pending_approval: Optional[ToolStep] = None
    total_cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0


async def run_tool_loop(
    route: Callable[[CompletionRequest], Awaitable[CompletionResponse]],
    registry: ToolRegistry,
    request: CompletionRequest,
    *,
    allowed: Optional[Iterable[str]],
    caller: str = "gateway",
    max_steps: int = DEFAULT_MAX_STEPS,
    max_cost_usd: Optional[float] = None,
    approved: Iterable[str] | Callable[[str, str, dict], bool] = (),
    max_permission: Permission = Permission.DESTRUCTIVE,
) -> ToolLoopResult:
    """Run the request, executing tool calls the model makes.

    Only tools in ``allowed`` are offered to the model (deny by default).
    A destructive tool not in ``approved`` stops the loop with
    ``needs_approval`` so the caller can ask a human and resume.
    """
    allowed = list(allowed or [])
    if not callable(approved):
        names = set(approved)
        approved = lambda caller, name, args: name in names  # noqa: E731
    specs = registry.specs(allowed)
    req = replace(
        request,
        messages=list(request.messages),
        tools=(request.tools or []) + specs if specs else request.tools,
        stream=False,
    )
    result = ToolLoopResult(response=None)  # type: ignore[arg-type]

    for _ in range(max_steps + 1):
        resp = await route(req)
        result.response = resp
        result.total_cost_usd += resp.cost_usd or 0.0
        result.input_tokens += resp.input_tokens or 0
        result.output_tokens += resp.output_tokens or 0
        calls = (resp.metadata or {}).get("tool_calls")
        if not calls:
            return result
        if len(result.steps) >= max_steps:
            result.stop_reason = "max_steps"
            return result
        if max_cost_usd is not None and result.total_cost_usd >= max_cost_usd:
            result.stop_reason = "cost_limit"
            return result

        req.messages.append(
            Message(role="assistant", content=resp.content or "", tool_calls=calls)
        )
        for call in calls:
            step = await _run_one(
                registry, call, allowed, caller, approved, max_permission
            )
            if step.error_code == "approval_required":
                result.stop_reason = "needs_approval"
                result.pending_approval = step
                result.steps.append(step)
                return result
            result.steps.append(step)
            req.messages.append(
                Message(role="tool", content=step.output, tool_call_id=step.call_id)
            )
    result.stop_reason = "max_steps"
    return result


async def _run_one(registry, call, allowed, caller, approved, max_permission) -> ToolStep:
    cid, name = call.get("id", ""), call.get("name", "")
    try:
        args = json.loads(call.get("arguments") or "{}")
        if not isinstance(args, dict):
            raise ValueError("arguments must be an object")
    except ValueError as exc:
        return ToolStep(cid, name, {}, False, f"Invalid arguments: {exc}", 0.0, "invalid_arguments")
    try:
        try:
            res = await registry.call(
                name, args, allowed=allowed, caller=caller,
                approved=False, max_permission=max_permission,
            )  # fmt: skip
        except ApprovalRequired:
            if not approved(caller, name, args):
                raise
            res = await registry.call(
                name, args, allowed=allowed, caller=caller,
                approved=True, max_permission=max_permission,
            )  # fmt: skip
        return ToolStep(cid, name, args, True, res.as_untrusted(), res.duration_ms)
    except ApprovalRequired as exc:
        return ToolStep(cid, name, args, False, str(exc), 0.0, exc.code)
    except ToolError as exc:
        # Errors go back to the model so it can correct itself.
        return ToolStep(cid, name, args, False, f"Tool error ({exc.code}): {exc}", 0.0, exc.code)
