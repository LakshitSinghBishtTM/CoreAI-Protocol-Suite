"""Supervise one ``llama-server`` child process per loaded model.

Load on first use, unload after an idle timeout, evict least-recently-used
when over the model limit, restart a crashed child with backoff. Children bind
to 127.0.0.1 only. ``clock`` and ``spawn`` are injectable for tests.
"""

import os
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

MAX_LOG_BYTES = 1_000_000


def _popen(cmd: list[str], log_path: str | None = None) -> Any:
    if not log_path:
        return subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    if os.path.exists(log_path) and os.path.getsize(log_path) > MAX_LOG_BYTES:
        os.replace(log_path, log_path + ".1")  # one rotation keeps disk bounded
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
    )
    thread = threading.Thread(
        target=_capture_log, args=(proc.stdout, log_path), daemon=True
    )
    thread.start()
    return _LoggedProcess(proc, thread)


def _capture_log(stream, log_path: str) -> None:
    """Drain child output while keeping the active log and one backup bounded."""
    size = os.path.getsize(log_path) if os.path.exists(log_path) else 0
    log = open(log_path, "ab")
    try:
        while True:
            chunk = stream.read(8192)
            if not chunk:
                break
            if size + len(chunk) > MAX_LOG_BYTES:
                log.close()
                if os.path.exists(log_path):
                    os.replace(log_path, log_path + ".1")
                log = open(log_path, "wb")
                size = 0
            if len(chunk) > MAX_LOG_BYTES:
                chunk = chunk[-MAX_LOG_BYTES:]
            log.write(chunk)
            size += len(chunk)
    finally:
        log.close()
        stream.close()


class _LoggedProcess:
    """Proxy Popen and join its log drainer after the child exits."""

    def __init__(self, process, log_thread: threading.Thread):
        self._process = process
        self._log_thread = log_thread

    def __getattr__(self, name):
        return getattr(self._process, name)

    def poll(self):
        result = self._process.poll()
        if result is not None:
            self._log_thread.join()
        return result

    def wait(self, timeout=None):
        result = self._process.wait(timeout=timeout)
        self._log_thread.join()
        return result


def tail_log(log_path: str, lines: int = 50) -> str:
    """Last ``lines`` lines of a model's log (empty string if none)."""
    try:
        with open(log_path, "rb") as f:
            f.seek(max(0, os.path.getsize(log_path) - 64_000))
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return ""
    return "\n".join(data.splitlines()[-lines:])


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
        log_dir: str | None = None,
    ):
        self.log_dir = log_dir
        self.max_loaded = max_loaded
        self.idle_timeout_s = idle_timeout_s
        self.max_restarts = max_restarts
        self.clock = clock
        self._spawn_fn = spawn
        self.port_fn = port_fn
        self.loaded: dict[str, Loaded] = {}
        self.failed: set[str] = set()

    def log_path(self, name: str) -> str | None:
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in name)
        return os.path.join(self.log_dir, f"{safe}.log") if self.log_dir else None

    def spawn(self, name: str, cmd: list[str]) -> Any:
        if self._spawn_fn:
            return self._spawn_fn(cmd)
        return _popen(cmd, self.log_path(name))

    def crash_log(self, name: str, lines: int = 50) -> str:
        p = self.log_path(name)
        return tail_log(p, lines) if p else ""

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
        self.loaded[name] = Loaded(
            name, port, self.spawn(name, cmd), self.clock(), cmd=cmd
        )
        return f"http://127.0.0.1:{port}/v1"

    def _restart(self, m: Loaded) -> None:
        if m.restarts >= self.max_restarts:
            self.failed.add(m.name)
            self.loaded.pop(m.name, None)
            tail = self.crash_log(m.name, 10)
            raise RuntimeError(
                f"model '{m.name}' crashed too often"
                + (f"; last log:\n{tail}" if tail else "")
            )
        if self.clock() < m.next_restart_at:
            raise RuntimeError(f"model '{m.name}' is restarting; retry shortly")
        m.restarts += 1
        m.next_restart_at = self.clock() + min(2**m.restarts, 60)
        m.proc = self.spawn(m.name, m.cmd)
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

    def unhealthy(self, healthy: Callable[[str], bool]) -> list[str]:
        """Names of live children whose health probe fails (hung, not crashed).
        They are killed so the next request restarts them with backoff."""
        bad = []
        for n, m in list(self.loaded.items()):
            if m.proc.poll() is None and not healthy(f"http://127.0.0.1:{m.port}/v1"):
                m.proc.kill()
                bad.append(n)
        return bad

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
