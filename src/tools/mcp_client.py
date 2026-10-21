"""tools/mcp_client.py

MCP client: connect to external MCP servers (a child process over stdio, or
an HTTP endpoint) and add their tools to the registry.

Safety rules:
- Deny by default. Only tools named in a server's ``allow`` list are
  registered (``"*"`` allows all of that server's tools).
- Tool names are namespaced ``<server>__<tool>`` so a remote tool can never
  shadow a built-in.
- Remote names, descriptions and schemas are untrusted: names are validated,
  descriptions are capped and flattened, invalid schemas are skipped.
- A stdio child gets a minimal environment (no provider keys) plus whatever
  the config lists under ``env``.
- Remote tools default to ``write`` permission, not ``read_only``, unless the
  config says otherwise.
"""

import asyncio
import itertools
import json
import os
import re
from typing import Any, Optional

import httpx
import jsonschema
from loguru import logger

from .mcp import PROTOCOL_VERSION
from .registry import Permission, Tool, ToolError, ToolRegistry

_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_DESCRIPTION = 500
REQUEST_TIMEOUT_S = 30.0


class McpClientError(Exception):
    pass


class StdioTransport:
    def __init__(
        self, command: list[str], env: Optional[dict] = None, cwd: Optional[str] = None
    ):
        self.command, self.cwd = command, cwd
        base = {
            k: os.environ[k]
            for k in ("PATH", "HOME", "LANG", "SYSTEMROOT")
            if k in os.environ
        }
        self.env = {**base, **(env or {})}
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            *self.command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=self.env, cwd=self.cwd,
        )  # fmt: skip

    async def send(self, message: dict, expect_reply: bool = True) -> Optional[dict]:
        proc = self._proc
        if proc is None or proc.returncode is not None:
            raise McpClientError("MCP server process is not running")
        async with self._lock:
            proc.stdin.write((json.dumps(message) + "\n").encode())
            await proc.stdin.drain()
            if not expect_reply:
                return None
            while True:  # skip anything that isn't the reply to this request
                line = await proc.stdout.readline()
                if not line:
                    raise McpClientError("MCP server closed the connection")
                try:
                    reply = json.loads(line)
                except ValueError:
                    continue
                if isinstance(reply, dict) and reply.get("id") == message.get("id"):
                    return reply

    async def close(self) -> None:
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.stdin.close()
            await asyncio.wait_for(proc.wait(), 3)
        except Exception:
            proc.kill()


class HttpTransport:
    def __init__(self, url: str, headers: Optional[dict] = None, transport=None):
        self.url, self.headers = url, headers or {}
        self._http = httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S, transport=transport)

    async def start(self) -> None:
        return None

    async def send(self, message: dict, expect_reply: bool = True) -> Optional[dict]:
        try:
            r = await self._http.post(self.url, json=message, headers=self.headers)
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise McpClientError(f"MCP HTTP request failed: {exc}") from exc
        if not expect_reply or r.status_code == 202 or not r.content:
            return None
        return r.json()

    async def close(self) -> None:
        await self._http.aclose()


class McpClient:
    def __init__(self, name: str, transport):
        self.name, self.transport = name, transport
        self._ids = itertools.count(1)

    async def _rpc(self, method: str, params: Optional[dict] = None) -> dict:
        msg = {"jsonrpc": "2.0", "id": next(self._ids), "method": method}
        if params is not None:
            msg["params"] = params
        try:
            reply = await asyncio.wait_for(self.transport.send(msg), REQUEST_TIMEOUT_S)
        except asyncio.TimeoutError as exc:
            raise McpClientError(f"{method} timed out") from exc
        if reply is None:
            raise McpClientError(f"no reply to {method}")
        if "error" in reply:
            raise McpClientError(f"{method}: {reply['error'].get('message', 'error')}")
        return reply.get("result") or {}

    async def connect(self) -> None:
        await self.transport.start()
        await self._rpc("initialize", {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "caips", "version": "0.1"},
        })  # fmt: skip
        await self.transport.send(
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            expect_reply=False,
        )

    async def list_tools(self) -> list[dict]:
        return (await self._rpc("tools/list")).get("tools") or []

    async def call_tool(self, name: str, arguments: dict) -> str:
        result = await self._rpc("tools/call", {"name": name, "arguments": arguments})
        text = "\n".join(
            c.get("text", "")
            for c in result.get("content") or []
            if c.get("type") == "text"
        )
        if result.get("isError"):
            raise ToolError("tool_failed", text or "remote tool failed")
        return text

    async def close(self) -> None:
        await self.transport.close()


def _clean_description(text: Any) -> str:
    flat = " ".join(str(text or "").split())
    return flat[:MAX_DESCRIPTION]


async def register_remote_tools(
    registry: ToolRegistry,
    client: McpClient,
    allow: list[str],
    permission: Permission = Permission.WRITE,
    timeout_s: float = 30.0,
) -> list[str]:
    """Register the allow-listed tools of a connected server. Returns the
    namespaced names that were added."""
    added = []
    wanted = set(allow)
    for spec in await client.list_tools():
        remote = spec.get("name")
        if not isinstance(remote, str) or not _NAME.match(remote):
            logger.warning(
                f"[mcp:{client.name}] skipping tool with bad name {remote!r}"
            )
            continue
        if "*" not in wanted and remote not in wanted:
            continue
        schema = spec.get("inputSchema") or {"type": "object"}
        try:
            jsonschema.Draft202012Validator.check_schema(schema)
        except jsonschema.SchemaError:
            logger.warning(f"[mcp:{client.name}] skipping '{remote}': invalid schema")
            continue

        async def handler(_remote=remote, **kwargs):
            return await client.call_tool(_remote, kwargs)

        local = f"{client.name}__{remote}"
        registry.register(
            Tool(
                local, _clean_description(spec.get("description")), schema, handler,
                owner=f"mcp:{client.name}", timeout_s=timeout_s, permission=permission,
            ),
            replace=True,
        )  # fmt: skip
        added.append(local)
    return added


async def connect_servers(registry: ToolRegistry, servers: dict) -> list[McpClient]:
    """Connect every configured server. One that fails is logged and skipped;
    it never stops startup. Returns the live clients (close them on shutdown)."""
    clients: list[McpClient] = []
    for name, cfg in (servers or {}).items():
        cfg = cfg or {}
        if not _NAME.match(str(name)) or "__" in str(name):
            logger.warning(f"[mcp] bad server name {name!r}; skipped")
            continue
        try:
            if cfg.get("url"):
                transport = HttpTransport(cfg["url"], cfg.get("headers"))
            elif cfg.get("command"):
                cmd = cfg["command"]
                transport = StdioTransport(
                    cmd if isinstance(cmd, list) else cmd.split(),
                    env=cfg.get("env"), cwd=cfg.get("cwd"),
                )  # fmt: skip
            else:
                raise ValueError("needs 'command' or 'url'")
            client = McpClient(name, transport)
            await client.connect()
            perm = Permission[str(cfg.get("permission", "write")).upper()]
            added = await register_remote_tools(
                registry, client, list(cfg.get("allow") or []), perm
            )
        except Exception as exc:
            logger.warning(f"[mcp:{name}] not connected: {exc}")
            continue
        logger.info(
            f"[mcp:{name}] connected, tools: {', '.join(added) or 'none (check allow)'}"
        )
        clients.append(client)
    return clients
