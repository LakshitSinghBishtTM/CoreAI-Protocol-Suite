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
