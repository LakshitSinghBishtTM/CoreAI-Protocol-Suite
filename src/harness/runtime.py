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


class HarnessRuntime:
    def __init__(self, runner: HarnessRunner):
        self.runner = runner
        self.tasks: dict[str, asyncio.Task] = {}

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
        return len(ids)

    async def stop(self) -> None:
        """Cancel live runs; each checkpoints as resumable before exiting."""
        for t in list(self.tasks.values()):
            t.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)


def build_runtime(cfg: dict, route, registry) -> HarnessRuntime:
    agents = {
        n: AgentDef.from_dict({"name": n, **d})
        for n, d in (cfg.get("agents") or {}).items()
    }
    store = RunStore(cfg.get("db", "data/runs.db"))
    return HarnessRuntime(HarnessRunner(route, registry, store, agents))


def load_harness_runtime(
    route, registry, path: Optional[str] = None
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
    return build_runtime(cfg, route, registry)
