import uuid
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from loguru import logger

from utils.clock import utcnow as _utcnow


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class AgentTask:
    """A task assigned to an agent"""

    task_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    agent_id: str = ""
    objective: str = ""
    context: dict = field(default_factory=dict)
    status: TaskStatus = TaskStatus.PENDING
    result: Optional[str] = None
    error: Optional[str] = None
    created_at: datetime = field(default_factory=_utcnow)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    iterations: int = 0
    max_iterations: int = 10
    priority: int = 2  # 0-3, higher runs first
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["created_at"] = self.created_at.isoformat()
        data["started_at"] = self.started_at.isoformat() if self.started_at else None
        data["completed_at"] = (
            self.completed_at.isoformat() if self.completed_at else None
        )
        data["status"] = self.status.value
        return data

    def duration_seconds(self) -> Optional[float]:
        if not self.started_at:
            return None
        end = self.completed_at or _utcnow()
        return (end - self.started_at).total_seconds()


class TaskStore:
    """
    In-memory task storage (replace with DB in production).

    Pending and running tasks are always kept. Finished tasks (completed,
    failed or cancelled) are kept only up to ``max_finished``; the oldest are
    dropped first, so the store cannot grow without bound.
    """

    _TERMINAL = (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED)

    def __init__(self, max_finished: int = 1000):
        self.tasks: dict[str, AgentTask] = {}
        self.max_finished = max_finished
        self._finished: OrderedDict[str, None] = OrderedDict()

    def _prune(self, task: AgentTask) -> None:
        if task.status not in self._TERMINAL:
            return
        self._finished[task.task_id] = None
        self._finished.move_to_end(task.task_id)
        while len(self._finished) > self.max_finished:
            old_id, _ = self._finished.popitem(last=False)
            self.tasks.pop(old_id, None)

    def create(
        self, agent_id: str, objective: str, context: dict = None, priority: int = 2
    ) -> AgentTask:
        task = AgentTask(
            agent_id=agent_id,
            objective=objective,
            context=context or {},
            priority=priority,
        )
        self.tasks[task.task_id] = task
        logger.info(f"Created task {task.task_id} for agent {agent_id}")
        return task

    def get(self, task_id: str) -> Optional[AgentTask]:
        return self.tasks.get(task_id)

    def update(self, task: AgentTask):
        self.tasks[task.task_id] = task
        self._prune(task)

    def list_by_agent(self, agent_id: str) -> list[AgentTask]:
        return [t for t in self.tasks.values() if t.agent_id == agent_id]

    def list_by_status(self, status: TaskStatus) -> list[AgentTask]:
        tasks = [t for t in self.tasks.values() if t.status == status]
        return sorted(tasks, key=lambda t: (-t.priority, t.created_at))

    def delete(self, task_id: str):
        self.tasks.pop(task_id, None)
        self._finished.pop(task_id, None)


class Orchestrator:
    """Manages agent lifecycle and task orchestration"""

    def __init__(self):
        self.task_store = TaskStore()
        self.agents: dict[str, dict[str, Any]] = {}
        self.active_tasks: dict[str, str] = {}  # task_id -> agent_id
        logger.info("Orchestrator initialized")

    def register_agent(self, agent_id: str, agent_config: dict = None):
        """Register a new agent"""
        self.agents[agent_id] = {
            "id": agent_id,
            "config": agent_config or {},
            "created_at": _utcnow(),
            "total_tasks": 0,
            "completed_tasks": 0,
            "failed_tasks": 0,
        }
        logger.info(f"Registered agent {agent_id}")

    def unregister_agent(self, agent_id: str):
        """Unregister an agent"""
        # Cancel any active tasks
        for task_id, aid in list(self.active_tasks.items()):
            if aid == agent_id:
                self.cancel_task(task_id)
        self.agents.pop(agent_id, None)
        logger.info(f"Unregistered agent {agent_id}")

    def assign_task(
        self, agent_id: str, objective: str, context: dict = None, priority: int = 2
    ) -> AgentTask:
        """Assign a task to an agent (priority 0-3, higher is more urgent)"""
        if agent_id not in self.agents:
            raise ValueError(f"Agent {agent_id} not registered")

        task = self.task_store.create(agent_id, objective, context, priority)
        self.active_tasks[task.task_id] = agent_id
        self.agents[agent_id]["total_tasks"] += 1

        logger.info(
            f"Assigned task {task.task_id} to agent {agent_id}: {objective[:50]}..."
        )
        return task

    def start_task(self, task_id: str):
        """Mark task as started"""
        task = self.task_store.get(task_id)
        if not task:
            raise ValueError(f"Task {task_id} not found")

        task.status = TaskStatus.RUNNING
        task.started_at = _utcnow()
        self.task_store.update(task)
        logger.debug(f"Started task {task_id}")

    def complete_task(self, task_id: str, result: str):
        """Mark task as completed"""
        task = self.task_store.get(task_id)
        if not task:
            raise ValueError(f"Task {task_id} not found")

        task.status = TaskStatus.COMPLETED
        task.result = result
        task.completed_at = _utcnow()
        self.task_store.update(task)

        agent_id = self.active_tasks.pop(task_id, None)
        if agent_id:
            self.agents[agent_id]["completed_tasks"] += 1

        logger.info(
            f"Completed task {task_id} (duration: {task.duration_seconds():.1f}s)"
        )

    def fail_task(self, task_id: str, error: str):
        """Mark task as failed"""
        task = self.task_store.get(task_id)
        if not task:
            raise ValueError(f"Task {task_id} not found")

        task.status = TaskStatus.FAILED
        task.error = error
        task.completed_at = _utcnow()
        self.task_store.update(task)

        agent_id = self.active_tasks.pop(task_id, None)
        if agent_id:
            self.agents[agent_id]["failed_tasks"] += 1

        logger.error(f"Failed task {task_id}: {error[:100]}")

    def cancel_task(self, task_id: str):
        """Cancel a task"""
        task = self.task_store.get(task_id)
        if not task:
            return

        task.status = TaskStatus.CANCELLED
        task.completed_at = _utcnow()
        self.task_store.update(task)
        self.active_tasks.pop(task_id, None)
        logger.info(f"Cancelled task {task_id}")

    def get_task(self, task_id: str) -> Optional[AgentTask]:
        return self.task_store.get(task_id)

    def get_agent_tasks(self, agent_id: str) -> list[AgentTask]:
        return self.task_store.list_by_agent(agent_id)

    def get_pending_tasks(self) -> list[AgentTask]:
        return self.task_store.list_by_status(TaskStatus.PENDING)

    def get_active_tasks(self) -> list[AgentTask]:
        return self.task_store.list_by_status(TaskStatus.RUNNING)

    def stats(self) -> dict:
        total_tasks = len(self.task_store.tasks)
        completed = len(self.task_store.list_by_status(TaskStatus.COMPLETED))
        failed = len(self.task_store.list_by_status(TaskStatus.FAILED))
        active = len(self.task_store.list_by_status(TaskStatus.RUNNING))

        return {
            "total_agents": len(self.agents),
            "total_tasks": total_tasks,
            "completed_tasks": completed,
            "failed_tasks": failed,
            "active_tasks": active,
            "pending_tasks": len(self.task_store.list_by_status(TaskStatus.PENDING)),
            "agent_stats": self.agents,
        }
