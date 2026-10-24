"""harness/runtime.py: server-side wiring (config file, background tasks).

Opt-in: without ``config/agents.yml`` there is no run API. Agents are defined
server-side; a caller can only start an agent whose tools it is granted.
"""

import asyncio
import os
from typing import Optional

from .config import AgentDef
from .runner import HarnessRunner
from .store import RunStore


def _bus():
    from utils.events import bus

    return bus


def build_triggers(cfg: dict, runner: HarnessRunner):
    """``triggers:`` block of agents.yml. Raises ValueError listing problems."""
    from .triggers import TriggerManager

    tm, problems = TriggerManager(runner), []
    for name, d in (cfg.get("triggers") or {}).items():
        try:
            extra = set(d) - {"every_s", "cron", "webhook_secret_env", "event", "agent", "task", "owner", "match", "max_per_hour"}
            if extra:
                raise ValueError(f"unknown fields {sorted(extra)}")
            kinds = [k for k in ("every_s", "cron", "webhook_secret_env", "event") if d.get(k)]
            if len(kinds) != 1:
                raise ValueError("set exactly one of every_s, cron, webhook_secret_env or event")
            common = dict(agent=d["agent"], task=d["task"], owner=d.get("owner", "local"),
                          max_per_hour=int(d.get("max_per_hour", 6)))  # fmt: skip
            if d.get("every_s"):
                tm.every(name, float(d["every_s"]), **common)
            elif d.get("cron"):
                tm.cron(name, str(d["cron"]), **common)
            elif d.get("webhook_secret_env"):
                import os

                tm.webhook(name, os.environ.get(d["webhook_secret_env"], ""), **common)
            else:
                tm.on_event(name, d["event"], match=d.get("match"), **common)
        except (KeyError, ValueError, TypeError) as exc:
            problems.append(f"trigger {name!r}: {exc}")
    if problems:
        raise ValueError("; ".join(problems))
    return tm


def build_workflows(cfg: dict) -> dict:
    """``workflows:`` block of agents.yml. Raises ValueError listing problems."""
    from .workflow import parse_steps

    out, problems = {}, []
    for name, d in (cfg.get("workflows") or {}).items():
        try:
            extra = set(d) - {"steps", "max_cost_usd"}
            if extra:
                raise ValueError(f"unknown fields {sorted(extra)}")
            cost = d.get("max_cost_usd")
            out[name] = {
                "steps": parse_steps(d.get("steps")),
                "max_cost_usd": float(cost) if cost is not None else None,
            }
        except (ValueError, TypeError, AttributeError) as exc:
            problems.append(f"workflow {name!r}: {exc}")
    if problems:
        raise ValueError("; ".join(problems))
    return out


class HarnessRuntime:
    def __init__(self, runner: HarnessRunner, triggers=None, workflows=None):
        self.runner = runner
        self.workflows: dict = workflows or {}  # name -> {"steps", "max_cost_usd"}
        self.tasks: dict[str, asyncio.Task] = {}
        self.triggers = triggers  # optional TriggerManager
        self._trigger_loop: Optional[asyncio.Task] = None

    def spawn(self, rid: str, decision: Optional[bool] = None) -> None:
        """Run in the background; one task per run."""
        if rid in self.tasks and not self.tasks[rid].done():
            return
        task = asyncio.create_task(self.runner.execute(rid, decision=decision))
        self.tasks[rid] = task
        task.add_done_callback(lambda t, r=rid: self.tasks.pop(r, None))

    def start(self) -> int:
        """Pick up runs a crash or shutdown interrupted."""
        ids = self.runner.store.interrupted()
        for rid in ids:
            self.spawn(rid)
        if self.triggers is not None and self.triggers.triggers:
            from utils.events import bus

            bus.subscribe(self.triggers.on_bus_event)
            self._trigger_loop = asyncio.create_task(self.triggers.run())
        return len(ids)

    async def stop(self) -> None:
        """Cancel live runs; each checkpoints as resumable before exiting."""
        if self._trigger_loop is not None:
            self._trigger_loop.cancel()
            await asyncio.gather(self._trigger_loop, return_exceptions=True)
            if self.triggers.on_bus_event in _bus().sinks:
                _bus().sinks.remove(self.triggers.on_bus_event)
        for t in list(self.tasks.values()):
            t.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)


def build_runtime(cfg: dict, route, registry, memory=None) -> HarnessRuntime:
    agents = {
        n: AgentDef.from_dict({"name": n, **d})
        for n, d in (cfg.get("agents") or {}).items()
    }
    store = RunStore(cfg.get("db", "data/runs.db"))
    runner = HarnessRunner(route, registry, store, agents, memory=memory)
    return HarnessRuntime(runner, build_triggers(cfg, runner), build_workflows(cfg))


def load_harness_runtime(
    route, registry, path: Optional[str] = None, memory=None
) -> Optional[HarnessRuntime]:
    path = path or os.getenv("COREAI_AGENTS_FILE", "config/agents.yml")
    if not os.path.exists(path):
        return None
    import yaml

    with open(path) as fh:
        cfg = yaml.safe_load(fh) or {}
    db = cfg.get("db", "data/runs.db")
    if db != ":memory:":
        os.makedirs(os.path.dirname(db) or ".", exist_ok=True)
    return build_runtime(cfg, route, registry, memory)
