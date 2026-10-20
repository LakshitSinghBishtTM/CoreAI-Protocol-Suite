"""tools/registry.py

Phase 6 core: a registry of tools with JSON schemas, versions, owners,
timeouts and permission levels. Every call is checked (allow list, schema,
approval for destructive tools), time-limited, and its output is capped and
labelled as untrusted data.

Layering: tools depend on nothing above them; the gateway and agents call in.
"""

import asyncio
import inspect
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Awaitable, Callable, Iterable, Optional

import jsonschema

MAX_OUTPUT_CHARS = 20_000


class Permission(IntEnum):
    READ_ONLY = 0
    WRITE = 1
    DESTRUCTIVE = 2


class ToolError(Exception):
    """A tool call was refused or failed. ``code`` is machine-readable."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class ApprovalRequired(ToolError):
    def __init__(self, tool: str):
        super().__init__("approval_required", f"Tool '{tool}' needs human approval.")
        self.tool = tool


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict  # JSON schema for the arguments
    handler: Callable[..., Any | Awaitable[Any]]
    version: str = "1"
    owner: str = "builtin"
    timeout_s: float = 30.0
    permission: Permission = Permission.READ_ONLY

    def openai_spec(self) -> dict:
        """The tool in the shape models and the gateway already use."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class ToolResult:
    tool: str
    content: str
    duration_ms: float
    truncated: bool = False
    # Tool output is data, never instructions. Consumers should wrap it with
    # ``as_untrusted()`` before showing it to a model.
    untrusted: bool = True

    def as_untrusted(self) -> str:
        return (
            f'<tool_output tool="{self.tool}" trust="untrusted">\n'
            f"{self.content}\n</tool_output>"
        )


@dataclass
class ToolRegistry:
    _tools: dict[str, Tool] = field(default_factory=dict)
    # Optional hook: called with (caller, tool, arguments, result_or_error,
    # duration_ms) for every call, for the audit log and traces.
    on_call: Optional[Callable[..., None]] = None

    def register(self, tool: Tool, replace: bool = False) -> None:
        if not tool.name or not tool.name.replace("_", "").replace("-", "").isalnum():
            raise ValueError(f"invalid tool name: {tool.name!r}")
        jsonschema.Draft202012Validator.check_schema(tool.parameters)
        if tool.name in self._tools and not replace:
            raise ValueError(f"tool '{tool.name}' is already registered")
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def visible(self, allowed: Optional[Iterable[str]] = None) -> list[Tool]:
        """Tools a caller may see. ``allowed=None`` means none are allowed
        (deny by default); pass ``["*"]`` for all."""
        if allowed is None:
            return []
        allow = set(allowed)
        return [t for n, t in sorted(self._tools.items()) if "*" in allow or n in allow]

    def specs(self, allowed: Optional[Iterable[str]] = None) -> list[dict]:
        return [t.openai_spec() for t in self.visible(allowed)]

    async def call(
        self,
        name: str,
        arguments: dict,
        *,
        allowed: Optional[Iterable[str]] = None,
        caller: str = "unknown",
        approved: bool = False,
        max_permission: Permission = Permission.DESTRUCTIVE,
    ) -> ToolResult:
        start = time.monotonic()
        try:
            result = await self._call(
                name, arguments, allowed, approved, max_permission, start
            )
        except Exception as exc:
            self._audit(caller, name, arguments, exc, start)
            raise
        self._audit(caller, name, arguments, result, start)
        return result

    async def _call(self, name, arguments, allowed, approved, max_permission, start):
        tool = self._tools.get(name)
        if tool is None:
            raise ToolError("unknown_tool", f"No such tool '{name}'.")
        allow = set(allowed or [])
        if "*" not in allow and name not in allow:
            raise ToolError("not_allowed", f"Tool '{name}' is not allowed for this caller.")
        if tool.permission > max_permission:
            raise ToolError(
                "permission_denied",
                f"Tool '{name}' needs {tool.permission.name} permission.",
            )
        if not isinstance(arguments, dict):
            raise ToolError("invalid_arguments", "Arguments must be a JSON object.")
        try:
            jsonschema.validate(arguments, tool.parameters)
        except jsonschema.ValidationError as exc:
            raise ToolError("invalid_arguments", exc.message) from exc
        if tool.permission is Permission.DESTRUCTIVE and not approved:
            raise ApprovalRequired(name)
        try:
            out = tool.handler(**arguments)
            if inspect.isawaitable(out):
                out = await asyncio.wait_for(out, timeout=tool.timeout_s)
        except asyncio.TimeoutError as exc:
            raise ToolError("timeout", f"Tool '{name}' timed out after {tool.timeout_s}s.") from exc
        except ToolError:
            raise
        except Exception as exc:
            raise ToolError("tool_failed", f"{type(exc).__name__}: {exc}") from exc
        text = out if isinstance(out, str) else _to_text(out)
        truncated = len(text) > MAX_OUTPUT_CHARS
        if truncated:
            text = text[:MAX_OUTPUT_CHARS] + "\n[output truncated]"
        return ToolResult(name, text, (time.monotonic() - start) * 1000, truncated)

    def _audit(self, caller, name, arguments, outcome, start) -> None:
        if self.on_call is None:
            return
        try:
            self.on_call(caller, name, arguments, outcome, (time.monotonic() - start) * 1000)
        except Exception:  # an audit hook must never break a tool call
            pass


def _to_text(value: Any) -> str:
    import json

    try:
        return json.dumps(value, default=str)
    except Exception:
        return str(value)
