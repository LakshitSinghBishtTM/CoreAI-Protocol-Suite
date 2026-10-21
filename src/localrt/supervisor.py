"""Supervise one ``llama-server`` child process per loaded model.

Load on first use, unload after an idle timeout, evict least-recently-used
when over the model limit, restart a crashed child with backoff. Children bind
to 127.0.0.1 only. ``clock`` and ``spawn`` are injectable for tests.
"""

import socket
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Callable


def _popen(cmd: list[str]) -> Any:
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def build_cmd(
    binary: str, model_path: str, port: int, plan: dict, parallel: int = 1
) -> list[str]:
    return [
        binary, "-m", model_path, "--host", "127.0.0.1", "--port", str(port),
        "-c", str(plan["ctx"] * parallel), "-ngl", str(plan["n_gpu_layers"]),
        "--parallel", str(parallel), "--jinja",
    ]  # fmt: skip


@dataclass
class Loaded:
    name: str
    port: int
    proc: Any
    last_used: float
    restarts: int = 0
    next_restart_at: float = 0.0
    cmd: list[str] = field(default_factory=list)


class Supervisor:
    def __init__(
        self,
        *,
        max_loaded: int = 1,
        idle_timeout_s: float = 600,
        max_restarts: int = 5,
        clock: Callable[[], float] = time.monotonic,
        spawn: Callable[[list[str]], Any] | None = None,
        port_fn: Callable[[], int] = free_port,
    ):
        self.max_loaded = max_loaded
        self.idle_timeout_s = idle_timeout_s
        self.max_restarts = max_restarts
        self.clock = clock
        self.spawn = spawn or _popen
        self.port_fn = port_fn
        self.loaded: dict[str, Loaded] = {}
        self.failed: set[str] = set()

    def ensure(self, name: str, binary: str, model_path: str, plan: dict) -> str:
        """Return the base URL for ``name``, starting or evicting as needed."""
        if name in self.failed:
            raise RuntimeError(f"model '{name}' crashed too often; reload manually")
        m = self.loaded.get(name)
        if m and m.proc.poll() is None:
            m.last_used = self.clock()
            return f"http://127.0.0.1:{m.port}/v1"
        if m:
            self._restart(m)
            return f"http://127.0.0.1:{m.port}/v1"
        while len(self.loaded) >= self.max_loaded:
            self.unload(min(self.loaded.values(), key=lambda x: x.last_used).name)
        port = self.port_fn()
        cmd = build_cmd(binary, model_path, port, plan)
        self.loaded[name] = Loaded(name, port, self.spawn(cmd), self.clock(), cmd=cmd)
        return f"http://127.0.0.1:{port}/v1"

    def _restart(self, m: Loaded) -> None:
        if m.restarts >= self.max_restarts:
            self.failed.add(m.name)
            self.loaded.pop(m.name, None)
            raise RuntimeError(f"model '{m.name}' crashed too often")
        if self.clock() < m.next_restart_at:
            raise RuntimeError(f"model '{m.name}' is restarting; retry shortly")
        m.restarts += 1
        m.next_restart_at = self.clock() + min(2**m.restarts, 60)
        m.proc = self.spawn(m.cmd)
        m.last_used = self.clock()

    def unload(self, name: str) -> bool:
        m = self.loaded.pop(name, None)
        if not m:
            return False
        if m.proc.poll() is None:
            m.proc.terminate()
            try:
                m.proc.wait(timeout=10)
            except Exception:
                m.proc.kill()
        return True

    def reap_idle(self) -> list[str]:
        now = self.clock()
        idle = [
            n for n, m in self.loaded.items() if now - m.last_used > self.idle_timeout_s
        ]
        for n in idle:
            self.unload(n)
        return idle

    def shutdown(self) -> None:
        for n in list(self.loaded):
            self.unload(n)
