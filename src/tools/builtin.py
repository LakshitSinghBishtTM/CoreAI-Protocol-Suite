"""tools/builtin.py

Phase 6 built-in tools: calculator, scoped file read/write, and HTTP fetch
with a domain allow list. Each is safe by construction: no eval, no path
escape from the workspace, no fetches to hosts that are not allow-listed or
that resolve to private addresses.
"""

import ast
import ipaddress
import math
import operator
import socket
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urlparse

import httpx

from .registry import Permission, Tool, ToolError, ToolRegistry

_BIN = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod, ast.Pow: operator.pow,
}  # fmt: skip
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCS = {
    "sqrt": math.sqrt, "abs": abs, "round": round, "min": min, "max": max,
    "sin": math.sin, "cos": math.cos, "tan": math.tan, "log": math.log,
    "log10": math.log10, "exp": math.exp, "floor": math.floor, "ceil": math.ceil,
}  # fmt: skip
_CONSTS = {"pi": math.pi, "e": math.e}
MAX_POW = 10_000


def safe_eval(expression: str) -> float | int:
    """Evaluate arithmetic only. No names, attributes or calls beyond _FUNCS."""
    if len(expression) > 500:
        raise ToolError("invalid_arguments", "Expression too long.")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ToolError("invalid_arguments", f"Bad expression: {exc.msg}") from exc

    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and type(n.value) in (int, float):
            return n.value
        if isinstance(n, ast.Name) and n.id in _CONSTS:
            return _CONSTS[n.id]
        if isinstance(n, ast.BinOp) and type(n.op) in _BIN:
            a, b = ev(n.left), ev(n.right)
            if isinstance(n.op, ast.Pow) and abs(b) > MAX_POW:
                raise ToolError("invalid_arguments", "Exponent too large.")
            return _BIN[type(n.op)](a, b)
        if isinstance(n, ast.UnaryOp) and type(n.op) in _UNARY:
            return _UNARY[type(n.op)](ev(n.operand))
        if (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id in _FUNCS
            and not n.keywords
        ):
            return _FUNCS[n.func.id](*[ev(a) for a in n.args])
        raise ToolError("invalid_arguments", "Only arithmetic is allowed.")

    try:
        return ev(tree)
    except ToolError:
        raise
    except (ZeroDivisionError, ValueError, OverflowError, TypeError) as exc:
        raise ToolError("invalid_arguments", f"{type(exc).__name__}: {exc}") from exc


class Workspace:
    """A directory tools may touch. Paths that resolve outside it are refused,
    including ``..`` and symlinks."""

    def __init__(self, root: str | Path, max_bytes: int = 1_000_000):
        self.root = Path(root).resolve()
        self.max_bytes = max_bytes

    def resolve(self, rel: str) -> Path:
        p = (self.root / rel).resolve()
        if p != self.root and self.root not in p.parents:
            raise ToolError("not_allowed", "Path is outside the workspace.")
        return p

    def read(self, path: str) -> str:
        p = self.resolve(path)
        if not p.is_file():
            raise ToolError("tool_failed", f"No such file: {path}")
        if p.stat().st_size > self.max_bytes:
            raise ToolError("tool_failed", "File too large.")
        return p.read_text(errors="replace")

    def write(self, path: str, content: str) -> str:
        if len(content.encode()) > self.max_bytes:
            raise ToolError("invalid_arguments", "Content too large.")
        p = self.resolve(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return f"wrote {len(content)} chars to {path}"


def _is_public(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            return False
    return True


class HttpFetcher:
    def __init__(
        self,
        allowed_domains: Iterable[str],
        transport: Optional[httpx.AsyncBaseTransport] = None,
        resolver=_is_public,
        max_bytes: int = 200_000,
    ):
        self.allowed = {d.lower().lstrip(".") for d in allowed_domains}
        self.transport = transport
        self.resolver = resolver
        self.max_bytes = max_bytes

    def _check(self, url: str) -> None:
        u = urlparse(url)
        host = (u.hostname or "").lower()
        if u.scheme not in ("http", "https") or not host:
            raise ToolError("invalid_arguments", "Only http(s) URLs are allowed.")
        if not any(host == d or host.endswith("." + d) for d in self.allowed):
            raise ToolError("not_allowed", f"Domain '{host}' is not allow-listed.")
        if not self.resolver(host):
            raise ToolError("not_allowed", f"Host '{host}' is not a public address.")

    async def fetch(self, url: str) -> str:
        self._check(url)
        async with httpx.AsyncClient(
            transport=self.transport, timeout=10, follow_redirects=False
        ) as http:
            r = await http.get(url)
        if 300 <= r.status_code < 400:  # a redirect could leave the allow list
            raise ToolError("not_allowed", "Redirects are not followed.")
        r.raise_for_status()
        return r.text[: self.max_bytes]


def register_builtins(
    registry: ToolRegistry,
    workspace: Optional[Workspace] = None,
    fetcher: Optional[HttpFetcher] = None,
) -> None:
    """Calculator always; files and HTTP only when configured (off by default)."""

    def obj(props: dict, req: list[str]) -> dict:
        return {
            "type": "object",
            "properties": props,
            "required": req,
            "additionalProperties": False,
        }

    registry.register(
        Tool(
            "calculator",
            "Evaluate an arithmetic expression.",
            obj({"expression": {"type": "string"}}, ["expression"]),
            lambda expression: str(safe_eval(expression)),
            timeout_s=2,
        )
    )
    if workspace:
        registry.register(
            Tool(
                "file_read",
                "Read a text file inside the workspace.",
                obj({"path": {"type": "string"}}, ["path"]),
                workspace.read,
            )
        )
        registry.register(
            Tool(
                "file_write",
                "Write a text file inside the workspace.",
                obj(
                    {"path": {"type": "string"}, "content": {"type": "string"}},
                    ["path", "content"],
                ),
                workspace.write,
                permission=Permission.WRITE,
            )
        )
    if fetcher:
        registry.register(
            Tool(
                "http_fetch",
                "GET an allow-listed URL and return its text.",
                obj({"url": {"type": "string"}}, ["url"]),
                fetcher.fetch,
                timeout_s=15,
            )
        )
