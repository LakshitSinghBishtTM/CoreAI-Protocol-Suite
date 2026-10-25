"""Response shapes for the gateway API, as ``TypedDict`` (plain dicts at runtime).

Fields marked ``NotRequired`` may be absent. The gateway can add fields, so
read only what you need.
"""

from __future__ import annotations

from typing import Any, Literal, NotRequired, Optional, TypedDict

RunStatus = Literal[
    "running", "paused", "waiting_approval", "completed", "failed", "cancelled"
]
ApprovalStatus = Literal["pending", "approved", "denied", "expired"]


class Tool(TypedDict):
    name: str
    description: str
    version: str
    owner: str
    permission: str  # read, write or destructive
    parameters: dict[str, Any]  # JSON Schema


class ToolContent(TypedDict):
    type: str
    text: str


class ToolResult(TypedDict):
    """Result of ``tools/call`` over MCP. ``isError`` is true for tool failures."""

    content: list[ToolContent]
    isError: bool


class PendingCall(TypedDict):
    calls: list[dict[str, Any]]


class Run(TypedDict):
    id: str
    agent: str
    owner: str
    status: str  # one of RunStatus
    stop_reason: Optional[str]
    step: int
    cost_usd: float
    input_tokens: int
    output_tokens: int
    control: str
    pending: Optional[PendingCall]
    result: Optional[str]
    created: float
    updated: float
    deadline: Optional[float]


class RunEvent(TypedDict):
    """One trace event. Extra keys depend on ``type`` (model_call, tool_call, ...)."""

    seq: int
    ts: float
    type: str


class Agent(TypedDict):
    name: str
    allowed_tools: list[str]
    approval_tools: list[str]
    max_steps: int
    max_cost_usd: Optional[float]
    model: Optional[str]


class Approval(TypedDict):
    id: str
    caller: str
    tool: str
    arguments: dict[str, Any]
    status: str  # one of ApprovalStatus
    decided_by: Optional[str]


class CancelResult(TypedDict):
    cancelled: bool


class PauseResult(TypedDict):
    pausing: bool


class KillResult(TypedDict):
    killed: int
    owner: Optional[str]


class ChatChoice(TypedDict):
    index: NotRequired[int]
    message: dict[str, Any]
    finish_reason: NotRequired[Optional[str]]


class ChatCompletion(TypedDict):
    id: NotRequired[str]
    model: NotRequired[str]
    choices: list[ChatChoice]
    usage: NotRequired[dict[str, int]]
