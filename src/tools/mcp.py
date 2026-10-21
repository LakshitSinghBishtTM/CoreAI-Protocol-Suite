"""tools/mcp.py

An MCP (Model Context Protocol) server over the tool registry. Speaks
JSON-RPC 2.0 with ``initialize``, ``ping``, ``tools/list`` and ``tools/call``,
so editors and agents can use CaiPS tools. Transport-agnostic: ``handle()``
takes one message and returns one reply (or None for notifications). A stdio
runner is included; the HTTP endpoint lives in the API layer.

Every call goes through the registry, so allow lists, schema validation,
approvals, rate limits and the audit hook apply exactly as for the gateway.
"""

import json
import sys
from typing import Any, Callable, Iterable, Optional

from .approvals import ApprovalStore
from .registry import ApprovalRequired, Permission, ToolError, ToolRegistry

PROTOCOL_VERSION = "2025-03-26"
SERVER_INFO = {"name": "caips-tools", "version": "0.1"}


def _err(mid: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _ok(mid: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


class McpServer:
    def __init__(
        self,
        registry: ToolRegistry,
        allowed_for: Callable[[str], Iterable[str]],
        approvals: Optional[ApprovalStore] = None,
        max_permission: Permission = Permission.WRITE,
    ):
        self.registry = registry
        self.allowed_for = allowed_for
        self.approvals = approvals
        self.max_permission = max_permission

    async def handle(self, message: Any, caller: str = "mcp") -> Optional[dict]:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _err(None, -32600, "Invalid request")
        method, mid = message.get("method"), message.get("id")
        params = message.get("params") or {}
        is_notification = "id" not in message
        if method == "initialize":
            return _ok(mid, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
            })  # fmt: skip
        if method == "ping":
            return _ok(mid, {})
        if is_notification:
            return None  # e.g. notifications/initialized
        if method == "tools/list":
            tools = self.registry.visible(self.allowed_for(caller))
            return _ok(mid, {"tools": [
                {"name": t.name, "description": t.description, "inputSchema": t.parameters}
                for t in tools
            ]})  # fmt: skip
        if method == "tools/call":
            return await self._call(mid, params, caller)
        return _err(mid, -32601, f"Method not found: {method}")

    async def _call(self, mid: Any, params: dict, caller: str) -> dict:
        name, args = params.get("name"), params.get("arguments") or {}
        if not isinstance(name, str):
            return _err(mid, -32602, "tools/call needs a tool name")
        allowed = list(self.allowed_for(caller))
        approved = bool(self.approvals and self.approvals.consume(caller, name, args))
        try:
            res = await self.registry.call(
                name, args, allowed=allowed, caller=caller,
                approved=approved, max_permission=self.max_permission,
            )  # fmt: skip
            return _ok(
                mid,
                {"content": [{"type": "text", "text": res.content}], "isError": False},
            )
        except ApprovalRequired:
            if self.approvals is None:
                return _tool_error(
                    mid,
                    "approval_required",
                    "This tool needs approval and none is configured.",
                )
            a = self.approvals.request(caller, name, args)
            return _tool_error(
                mid, "approval_required",
                f"Needs human approval (id {a.id}). Ask an admin to approve it, then repeat the call.",
            )  # fmt: skip
        except ToolError as exc:
            return _tool_error(mid, exc.code, str(exc))


def _tool_error(mid: Any, code: str, text: str) -> dict:
    return _ok(
        mid, {"content": [{"type": "text", "text": f"{code}: {text}"}], "isError": True}
    )


async def serve_stdio(server: McpServer, caller: str = "stdio") -> None:
    """Newline-delimited JSON-RPC on stdin/stdout (the MCP stdio transport)."""
    import asyncio

    loop = asyncio.get_running_loop()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            return
        line = line.strip()
        if not line:
            continue
        try:
            reply = await server.handle(json.loads(line), caller)
        except json.JSONDecodeError:
            reply = _err(None, -32700, "Parse error")
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()
