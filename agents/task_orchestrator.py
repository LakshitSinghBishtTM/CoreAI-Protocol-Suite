"""
agents/task_orchestrator.py

Distributes and schedules tasks across available CoreAI agents.
Handles priority queuing, load balancing, retries, and result aggregation.

Contact: ops@coreai.com
"""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from agents.agent_manager import AgentManager

logger = logging.getLogger(__name__)


class TaskPriority(IntEnum):
    CRITICAL = 0
    HIGH = 1
    NORMAL = 2
    LOW = 3


class TaskStatus:
    QUEUED = "queued"
    ASSIGNED = "assigned"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(order=True)
class TaskItem:
    priority: int
    created_at: float = field(compare=True)
    task_id: str = field(compare=False)
    payload: Dict = field(compare=False)
    retries: int = field(default=0, compare=False)
    max_retries: int = field(default=3, compare=False)
    assigned_to: Optional[str] = field(default=None, compare=False)
    status: str = field(default=TaskStatus.QUEUED, compare=False)
    result: Any = field(default=None, compare=False)
    error: Optional[str] = field(default=None, compare=False)


class TaskOrchestrator:
    """
    Routes tasks to available agents with priority queuing and retry logic.
    Supports parallel dispatch and result fan-in for multi-agent workflows.
    """

    MAX_QUEUE_SIZE = 512
    DISPATCH_INTERVAL = 0.1  # seconds between dispatch cycles
    DEFAULT_TIMEOUT = 120  # seconds per task

    def __init__(self, agent_manager: "AgentManager"):
        self.agent_manager = agent_manager
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue(
            maxsize=self.MAX_QUEUE_SIZE
        )
        self._tasks: Dict[str, TaskItem] = {}
        self._results: Dict[str, asyncio.Future] = {}
        self._dispatch_task: Optional[asyncio.Task] = None
        self._running = False
        logger.info("TaskOrchestrator initialized")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._running = True
        self._dispatch_task = asyncio.create_task(self._dispatch_loop())
        logger.info("TaskOrchestrator dispatch loop started")

    async def stop(self) -> None:
        self._running = False
        if self._dispatch_task:
            self._dispatch_task.cancel()
        # Cancel all pending futures
        for task_id, fut in self._results.items():
            if not fut.done():
                fut.cancel()
        logger.info("TaskOrchestrator stopped")

    # ------------------------------------------------------------------
    # Task submission
    # ------------------------------------------------------------------

    async def submit(
        self,
        instruction: str,
        priority: TaskPriority = TaskPriority.NORMAL,
        context: Optional[Dict] = None,
        constraints: Optional[List[str]] = None,
        max_retries: int = 3,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> str:
        task_id = f"task-{uuid.uuid4().hex[:10]}"

        item = TaskItem(
            priority=int(priority),
            created_at=time.monotonic(),
            task_id=task_id,
            payload={
                "instruction": instruction,
                "context": context or {},
                "constraints": constraints or [],
                "timeout": timeout,
            },
            max_retries=max_retries,
        )

        loop = asyncio.get_event_loop()
        self._tasks[task_id] = item
        self._results[task_id] = loop.create_future()

        await self._queue.put(item)
        logger.debug("Task %s queued (priority=%s)", task_id, priority.name)
        return task_id

    async def submit_and_wait(
        self,
        instruction: str,
        priority: TaskPriority = TaskPriority.NORMAL,
        **kwargs,
    ) -> Any:
        task_id = await self.submit(instruction, priority, **kwargs)
        return await self.get_result(task_id)

    # ------------------------------------------------------------------
    # Result retrieval
    # ------------------------------------------------------------------

    async def get_result(self, task_id: str, timeout: Optional[float] = None) -> Any:
        fut = self._results.get(task_id)
        if not fut:
            raise KeyError(f"Unknown task: {task_id}")
        try:
            return await asyncio.wait_for(asyncio.shield(fut), timeout=timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(f"Task {task_id} did not complete within {timeout}s")

    def get_status(self, task_id: str) -> Dict:
        item = self._tasks.get(task_id)
        if not item:
            raise KeyError(f"Unknown task: {task_id}")
        return {
            "task_id": item.task_id,
            "status": item.status,
            "priority": TaskPriority(item.priority).name,
            "retries": item.retries,
            "assigned_to": item.assigned_to,
            "error": item.error,
        }

    def cancel(self, task_id: str) -> bool:
        item = self._tasks.get(task_id)
        if not item or item.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
            return False
        item.status = TaskStatus.CANCELLED
        fut = self._results.get(task_id)
        if fut and not fut.done():
            fut.cancel()
        logger.info("Task %s cancelled", task_id)
        return True

    # ------------------------------------------------------------------
    # Fan-out / parallel dispatch
    # ------------------------------------------------------------------

    async def broadcast(
        self,
        instruction: str,
        agent_ids: List[str],
        context: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """Dispatch the same task to multiple agents and collect all results."""
        tasks = {
            aid: asyncio.create_task(
                self.agent_manager.dispatch_task(
                    aid,
                    {"instruction": instruction, "context": context or {}},
                )
            )
            for aid in agent_ids
        }
        results = {}
        for aid, coro in tasks.items():
            try:
                results[aid] = await coro
            except Exception as exc:
                results[aid] = {"error": str(exc)}
        return results

    # ------------------------------------------------------------------
    # Dispatch loop
    # ------------------------------------------------------------------

    async def _dispatch_loop(self) -> None:
        while self._running:
            try:
                idle_agents = self.agent_manager.get_idle_agents()
                if idle_agents and not self._queue.empty():
                    item: TaskItem = self._queue.get_nowait()
                    agent_id = idle_agents[0]
                    asyncio.create_task(self._run_task(item, agent_id))
            except asyncio.QueueEmpty:
                pass
            except Exception as exc:
                logger.error("Dispatch loop error: %s", exc)

            await asyncio.sleep(self.DISPATCH_INTERVAL)

    async def _run_task(self, item: TaskItem, agent_id: str) -> None:
        item.assigned_to = agent_id
        item.status = TaskStatus.RUNNING
        fut = self._results.get(item.task_id)

        try:
            timeout = item.payload.get("timeout", self.DEFAULT_TIMEOUT)
            result = await asyncio.wait_for(
                self.agent_manager.dispatch_task(agent_id, item.payload),
                timeout=timeout,
            )
            item.status = TaskStatus.COMPLETED
            item.result = result
            if fut and not fut.done():
                fut.set_result(result)
            logger.info("Task %s completed by agent %s", item.task_id, agent_id)

        except asyncio.TimeoutError:
            await self._handle_failure(
                item, fut, f"Timed out after {item.payload.get('timeout')}s"
            )

        except Exception as exc:
            await self._handle_failure(item, fut, str(exc))

    async def _handle_failure(
        self,
        item: TaskItem,
        fut: Optional[asyncio.Future],
        error: str,
    ) -> None:
        item.error = error
        item.retries += 1
        logger.warning(
            "Task %s failed (attempt %d/%d): %s",
            item.task_id,
            item.retries,
            item.max_retries,
            error,
        )

        if item.retries < item.max_retries:
            item.status = TaskStatus.QUEUED
            item.assigned_to = None
            await self._queue.put(item)
            logger.info("Task %s requeued for retry", item.task_id)
        else:
            item.status = TaskStatus.FAILED
            if fut and not fut.done():
                fut.set_exception(
                    RuntimeError(f"Task failed after {item.retries} attempts: {error}")
                )

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def queue_depth(self) -> int:
        return self._queue.qsize()

    def list_tasks(self, status_filter: Optional[str] = None) -> List[Dict]:
        tasks = list(self._tasks.values())
        if status_filter:
            tasks = [t for t in tasks if t.status == status_filter]
        return [self.get_status(t.task_id) for t in tasks]
