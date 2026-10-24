"""Sandboxed Python execution (opt-in, DESTRUCTIVE so it needs approval).

Each run is a fresh child process in a throwaway directory with an empty
environment, ``python -I``, CPU/memory/file-size/process rlimits and a wall
clock timeout. On Linux the child also gets its own network namespace
(``unshare -rn``) when available, so it has no network. If no network
isolation can be set up, the tool refuses to run unless ``allow_network=True``
is set explicitly. This is process-level isolation, not a container: use a
container or microVM runner for hostile code.
"""

import asyncio
import os
import resource
import shutil
import sys
import tempfile
from typing import Optional

from .registry import Permission, Tool, ToolError, ToolRegistry

MAX_OUTPUT = 20_000


class PythonSandbox:
    def __init__(
        self,
        timeout_s: float = 10.0,
        memory_mb: int = 256,
        cpu_s: int = 10,
        allow_network: bool = False,
    ):
        self.timeout_s, self.memory_mb, self.cpu_s = timeout_s, memory_mb, cpu_s
        self.allow_network = allow_network
        self._unshare = shutil.which("unshare")

    def _limits(self):
        mem = self.memory_mb * 1024 * 1024

        def apply():
            resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
            resource.setrlimit(resource.RLIMIT_CPU, (self.cpu_s, self.cpu_s))
            resource.setrlimit(resource.RLIMIT_FSIZE, (10 * 1024 * 1024,) * 2)
            resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))
            os.setsid()

        return apply

    def _argv(self, script: str) -> list[str]:
        base = [sys.executable, "-I", script]
        if self.allow_network:
            return base
        return ["unshare", "-rn", *base] if self._unshare else []

    async def run(self, code: str) -> str:
        argv_probe = self._argv("x")
        if not argv_probe:
            raise ToolError(
                "sandbox_unavailable",
                "No network isolation available (unshare missing); refusing to run.",
            )
        with tempfile.TemporaryDirectory(prefix="caips-sbx-") as d:
            script = os.path.join(d, "main.py")
            with open(script, "w") as f:
                f.write(code)
            proc = await asyncio.create_subprocess_exec(
                *self._argv(script), cwd=d, env={"PATH": "/usr/bin:/bin"},
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                preexec_fn=self._limits(),
            )  # fmt: skip

            async def read_output():
                out = bytearray()
                truncated = False
                while True:
                    chunk = await proc.stdout.read(4096)
                    if not chunk:
                        break
                    remaining = MAX_OUTPUT - len(out)
                    out.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        truncated = True
                        break
                if truncated:
                    try:
                        os.killpg(proc.pid, 9)
                    except ProcessLookupError:
                        pass
                await proc.wait()
                return bytes(out), truncated

            try:
                out, truncated = await asyncio.wait_for(
                    read_output(), timeout=self.timeout_s
                )
            except asyncio.TimeoutError:
                try:
                    os.killpg(proc.pid, 9)
                except ProcessLookupError:
                    pass
                await proc.wait()
                raise ToolError("timeout", f"Code exceeded {self.timeout_s:g}s.")
        text = out[:MAX_OUTPUT].decode("utf-8", "replace")
        if truncated:
            text += "\n[output truncated]"
        return f"exit_code={proc.returncode}\n{text}"


def register_sandbox(registry: ToolRegistry, sandbox: Optional[PythonSandbox]) -> None:
    if sandbox is None:
        return
    registry.register(Tool(
        "python_run", "Run Python code in a sandbox (no network, limited CPU/memory). Needs approval.",
        {"type": "object", "properties": {"code": {"type": "string", "maxLength": 20000}},
         "required": ["code"], "additionalProperties": False},
        sandbox.run, timeout_s=sandbox.timeout_s + 5, permission=Permission.DESTRUCTIVE,
    ))  # fmt: skip
