"""Runs API tasks on the agents the AgentManager owns.

One lifecycle: the Orchestrator task store is the source of truth for status
(pending -> running -> completed / failed / cancelled); this runner executes
the work. Each agent has its own priority queue (higher ``priority`` first,
FIFO within a priority) and runs one task at a time, because an agent keeps
a single conversation context.
"""

import asyncio
import itertools
from typing import Optional

from loguru import logger

from .orchestrator import Orchestrator, TaskStatus

DEFAULT_TASK_TIMEOUT_S = 120.0


class TaskRunner:
    def __init__(
        self,
        orchestrator: Orchestrator,
        agent_manager,
        timeout_s: float = DEFAULT_TASK_TIMEOUT_S,
    ):
        self.orch = orchestrator
        self.manager = agent_manager
        self.timeout_s = timeout_s
        self._queues: dict[str, asyncio.PriorityQueue] = {}
        self._workers: dict[str, asyncio.Task] = {}
        self._current: dict[str, tuple[str, asyncio.Task]] = (
            {}
        )  # agent -> (task_id, run)
        self._seq = itertools.count()

    def _task(self, task_id: str):
        task = self.orch.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        return task

    def submit(self, task_id: str) -> None:
        task = self._task(task_id)
        q = self._queues.setdefault(task.agent_id, asyncio.PriorityQueue())
        q.put_nowait((-task.priority, next(self._seq), task_id))
        worker = self._workers.get(task.agent_id)
        if worker is None or worker.done():
            self._workers[task.agent_id] = asyncio.create_task(
                self._work(task.agent_id)
            )

    async def _work(self, agent_id: str) -> None:
        q = self._queues[agent_id]
        while True:
            _, _, task_id = await q.get()
            task = self.orch.get_task(task_id)
            if task is None or task.status != TaskStatus.PENDING:
                continue  # cancelled while queued
            run = asyncio.create_task(self._run(task_id, agent_id))
            self._current[agent_id] = (task_id, run)
            try:
                # wait() does not raise when ``run`` alone is cancelled (a task
                # cancel); it raises only if this worker is cancelled.
                await asyncio.wait({run})
            except asyncio.CancelledError:
                run.cancel()
                await asyncio.gather(run, return_exceptions=True)
                raise
            finally:
                self._current.pop(agent_id, None)

    async def _run(self, task_id: str, agent_id: str) -> None:
        task = self._task(task_id)
        try:
            record = self.manager.agents.get(agent_id)
            if record is None:
                raise RuntimeError(f"Agent {agent_id} is no longer available")
            self.orch.start_task(task_id)

            def _progress(n: int, _t=task) -> None:
                _t.iterations = n

            result = await asyncio.wait_for(
                self.manager.dispatch_task(
                    agent_id,
                    {
                        "instruction": task.objective,
                        "context": task.context,
                        "max_steps": task.max_iterations,
                        "on_step": _progress,
                    },
                ),
                timeout=self.timeout_s,
            )
            if result is None:
                raise RuntimeError("Agent finished without a final result")
            if self._still_running(task_id):
                self.orch.complete_task(task_id, str(result))
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            self._fail(task_id, f"Task timed out after {self.timeout_s:.0f}s")
        except Exception as exc:
            logger.error("Task {} failed: {}", task_id, exc)
            self._fail(task_id, f"{type(exc).__name__}: {exc}")

    def _still_running(self, task_id: str) -> bool:
        t = self.orch.get_task(task_id)
        return t is not None and t.status == TaskStatus.RUNNING

    def _fail(self, task_id: str, error: str) -> None:
        if self._still_running(task_id) or (
            (t := self.orch.get_task(task_id)) is not None
            and t.status == TaskStatus.PENDING
        ):
            self.orch.fail_task(task_id, error)

    def cancel(self, task_id: str) -> bool:
        """Cancel a pending or running task. False if unknown or already done."""
        task = self.orch.get_task(task_id)
        if task is None or task.status not in (TaskStatus.PENDING, TaskStatus.RUNNING):
            return False
        self.orch.cancel_task(task_id)
        cur = self._current.get(task.agent_id)
        if cur and cur[0] == task_id:
            cur[1].cancel()
        return True

    async def drop_agent(self, agent_id: str) -> None:
        """Stop an agent's worker and cancel its tasks (agent terminated)."""
        for t in list(self.orch.get_agent_tasks(agent_id)):
            self.cancel(t.task_id)
        worker = self._workers.pop(agent_id, None)
        if worker:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        self._queues.pop(agent_id, None)
        self.orch.unregister_agent(agent_id)

    async def shutdown(self) -> None:
        for agent_id in list(self._workers):
            await self.drop_agent(agent_id)


def get_task_runner() -> Optional[TaskRunner]:
    from api.server import app_state

    return app_state.get("task_runner")
