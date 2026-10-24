import pytest

from localrt.supervisor import Supervisor, build_cmd

PLAN = {"ctx": 4096, "n_gpu_layers": 20, "fits": True}


class FakeProc:
    def __init__(self):
        self.alive = True
        self.terminated = False

    def poll(self):
        return None if self.alive else 1

    def terminate(self):
        self.alive = False
        self.terminated = True

    def wait(self, timeout=None):
        pass

    def kill(self):
        self.alive = False


class Env:
    def __init__(self, **kw):
        self.t = 0.0
        self.procs = []
        ports = iter(range(9000, 9100))
        self.sup = Supervisor(
            clock=lambda: self.t, spawn=self._spawn, port_fn=lambda: next(ports), **kw
        )

    def _spawn(self, cmd):
        p = FakeProc()
        self.procs.append(p)
        return p


def test_cmd_binds_localhost():
    c = build_cmd("llama-server", "/m.gguf", 9000, PLAN)
    assert c[c.index("--host") + 1] == "127.0.0.1" and "-ngl" in c


def test_lazy_load_and_reuse():
    e = Env()
    u = e.sup.ensure("a", "bin", "/a.gguf", PLAN)
    assert u == "http://127.0.0.1:9000/v1"
    assert e.sup.ensure("a", "bin", "/a.gguf", PLAN) == u and len(e.procs) == 1


def test_lru_eviction():
    e = Env(max_loaded=2)
    e.sup.ensure("a", "b", "/a", PLAN)
    e.t = 1
    e.sup.ensure("b", "b", "/b", PLAN)
    e.t = 2
    e.sup.ensure("a", "b", "/a", PLAN)  # a is now most recent
    e.sup.ensure("c", "b", "/c", PLAN)
    assert set(e.sup.loaded) == {"a", "c"} and e.procs[1].terminated


def test_idle_unload():
    e = Env(idle_timeout_s=60)
    e.sup.ensure("a", "b", "/a", PLAN)
    e.t = 30
    assert e.sup.reap_idle() == []
    e.t = 100
    assert e.sup.reap_idle() == ["a"] and not e.sup.loaded


def test_crash_restart_with_backoff_then_give_up():
    e = Env(max_restarts=2)
    e.sup.ensure("a", "b", "/a", PLAN)
    e.procs[-1].alive = False
    e.sup.ensure("a", "b", "/a", PLAN)  # restart 1
    assert len(e.procs) == 2
    e.procs[-1].alive = False
    with pytest.raises(RuntimeError, match="restarting"):
        e.sup.ensure("a", "b", "/a", PLAN)  # still in backoff
    e.t = 10
    e.sup.ensure("a", "b", "/a", PLAN)  # restart 2
    e.procs[-1].alive = False
    e.t = 100
    with pytest.raises(RuntimeError, match="too often"):
        e.sup.ensure("a", "b", "/a", PLAN)
    with pytest.raises(RuntimeError, match="reload"):
        e.sup.ensure("a", "b", "/a", PLAN)


def test_unhealthy_child_is_killed_then_restarted():
    e = Env()
    e.sup.ensure("a", "b", "/a", PLAN)
    assert e.sup.unhealthy(lambda url: True) == []
    assert e.sup.unhealthy(lambda url: False) == ["a"]
    assert not e.procs[0].alive
    e.sup.ensure("a", "b", "/a", PLAN)  # polled dead -> restart
    assert len(e.procs) == 2


def test_real_log_capture_and_tail(tmp_path):
    import sys

    from localrt.supervisor import Supervisor, tail_log

    sup = Supervisor(log_dir=str(tmp_path))
    proc = sup.spawn(
        "m/odel", [sys.executable, "-c", "print('boom'); raise SystemExit(3)"]
    )
    proc.wait(timeout=10)
    assert proc.returncode == 3
    assert "boom" in sup.crash_log("m/odel")
    assert tail_log(str(tmp_path / "nope.log")) == ""


def test_child_logs_rotate_while_the_process_is_running(tmp_path):
    import sys
    import time

    from localrt.supervisor import MAX_LOG_BYTES, Supervisor

    sup = Supervisor(log_dir=str(tmp_path))
    proc = sup.spawn(
        "large",
        [
            sys.executable,
            "-c",
            f"import sys, time; sys.stdout.buffer.write(b'x' * {MAX_LOG_BYTES * 3}); sys.stdout.flush(); time.sleep(0.5)",
        ],
    )
    current = tmp_path / "large.log"
    previous = tmp_path / "large.log.1"
    deadline = time.monotonic() + 5
    while not previous.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert proc.poll() is None
    assert current.stat().st_size <= MAX_LOG_BYTES
    assert previous.stat().st_size <= MAX_LOG_BYTES
    proc.wait(timeout=10)
    assert current.stat().st_size <= MAX_LOG_BYTES
    assert previous.stat().st_size <= MAX_LOG_BYTES
    assert current.stat().st_size + previous.stat().st_size <= MAX_LOG_BYTES * 2


def test_give_up_error_includes_log_tail(tmp_path):
    from localrt.supervisor import Supervisor

    (tmp_path / "a.log").write_text("CUDA out of memory\n")
    t = [0.0]
    sup = Supervisor(
        max_restarts=0, clock=lambda: t[0], spawn=lambda c: FakeProc(),
        port_fn=lambda: 9000, log_dir=str(tmp_path),
    )  # fmt: skip
    sup.ensure("a", "b", "/a", PLAN)
    sup.loaded["a"].proc.alive = False
    with pytest.raises(RuntimeError, match="CUDA out of memory"):
        sup.ensure("a", "b", "/a", PLAN)
