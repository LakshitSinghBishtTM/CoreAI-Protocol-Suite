"""
runtime/distributed_runtime.py

Distributed runtime coordinator for CoreAI Protocol Suite.
Manages worker node registration, heartbeat tracking, task fan-out,
and result aggregation across a multi-node deployment.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

COORDINATOR_HOST = os.environ.get("COREAI_COORDINATOR_HOST", "0.0.0.0")
COORDINATOR_PORT = int(os.environ.get("COREAI_COORDINATOR_PORT", "7420"))
HEARTBEAT_INTERVAL_S = float(os.environ.get("COREAI_HEARTBEAT_INTERVAL", "5.0"))
NODE_TIMEOUT_S = float(os.environ.get("COREAI_NODE_TIMEOUT", "30.0"))
MAX_WORKERS = int(os.environ.get("COREAI_MAX_WORKERS", "16"))
REPLICATION_FACTOR = int(os.environ.get("COREAI_REPLICATION_FACTOR", "2"))


# ---------------------------------------------------------------------------
# Enums / dataclasses
# ---------------------------------------------------------------------------


class NodeStatus(str, Enum):
    ONLINE = "online"
    DEGRADED = "degraded"
    OFFLINE = "offline"
    DRAINING = "draining"


class TaskStatus(str, Enum):
    PENDING = "pending"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    RETRYING = "retrying"


@dataclass
class WorkerNode:
    node_id: str
    host: str
    port: int
    status: NodeStatus = NodeStatus.ONLINE
    last_heartbeat: float = field(default_factory=time.time)
    active_tasks: int = 0
    total_tasks_handled: int = 0
    error_count: int = 0
    region: str = "us-east-1"
    weight: float = 1.0

    @property
    def is_healthy(self) -> bool:
        return (
            self.status == NodeStatus.ONLINE
            and (time.time() - self.last_heartbeat) < NODE_TIMEOUT_S
        )

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "address": self.address,
            "status": self.status,
            "active_tasks": self.active_tasks,
            "total_tasks_handled": self.total_tasks_handled,
            "error_count": self.error_count,
            "region": self.region,
            "last_heartbeat_age_s": round(time.time() - self.last_heartbeat, 1),
        }


@dataclass
class DistributedTask:
    task_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    payload: dict = field(default_factory=dict)
    status: TaskStatus = TaskStatus.PENDING
    assigned_node: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    result: Any = None
    error: Optional[str] = None
    attempt: int = 0
    max_attempts: int = 3
    checksum: str = ""

    def __post_init__(self):
        if not self.checksum:
            raw = str(self.payload).encode()
            self.checksum = hashlib.sha256(raw).hexdigest()[:16]

    @property
    def duration_ms(self) -> Optional[float]:
        if self.started_at and self.completed_at:
            return round((self.completed_at - self.started_at) * 1000, 2)
        return None


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class NoHealthyWorkersError(RuntimeError):
    """Raised when task dispatch finds zero healthy workers."""


class TaskDispatchError(RuntimeError):
    """Raised when a task cannot be dispatched after max retries."""


class NodeRegistrationError(ValueError):
    """Raised when a worker node fails registration validation."""


# ---------------------------------------------------------------------------
# Load balancer
# ---------------------------------------------------------------------------


class WeightedRoundRobin:
    """
    Weighted round-robin selector over healthy worker nodes.
    Nodes with higher weight receive proportionally more tasks.
    """

    def __init__(self):
        self._index = 0
        self._current_weights: dict[str, float] = {}

    def select(self, nodes: list[WorkerNode]) -> WorkerNode:
        healthy = [n for n in nodes if n.is_healthy and n.status != NodeStatus.DRAINING]
        if not healthy:
            raise NoHealthyWorkersError("No healthy workers available for dispatch")

        # Weighted selection: prefer nodes with lower active task count
        scored = sorted(healthy, key=lambda n: n.active_tasks / max(n.weight, 0.01))
        selected = scored[self._index % len(scored)]
        self._index += 1
        return selected


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------


class DistributedCoordinator:
    """
    Central coordinator for the CoreAI distributed runtime.

    Responsibilities:
      - Worker node registry and health tracking
      - Task fan-out with weighted load balancing
      - Result aggregation and fan-in
      - Heartbeat monitoring with automatic eviction
    """

    def __init__(self):
        self._nodes: dict[str, WorkerNode] = {}
        self._tasks: dict[str, DistributedTask] = {}
        self._lb = WeightedRoundRobin()
        self._running = False
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._dispatch_callbacks: list[Callable] = []
        self._stats = {
            "dispatched": 0,
            "completed": 0,
            "failed": 0,
            "retried": 0,
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        logger.info(
            "DistributedCoordinator started on %s:%d",
            COORDINATOR_HOST,
            COORDINATOR_PORT,
        )

    async def stop(self) -> None:
        self._running = False
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
        logger.info("DistributedCoordinator stopped")

    # ------------------------------------------------------------------
    # Node management
    # ------------------------------------------------------------------

    def register_node(
        self,
        host: str,
        port: int,
        region: str = "us-east-1",
        weight: float = 1.0,
    ) -> WorkerNode:
        if not host or not (1 <= port <= 65535):
            raise NodeRegistrationError(f"Invalid node address: {host}:{port}")

        node_id = hashlib.md5(f"{host}:{port}".encode()).hexdigest()[:12]

        if node_id in self._nodes:
            logger.debug("Node %s re-registered — resetting heartbeat", node_id)
            self._nodes[node_id].last_heartbeat = time.time()
            self._nodes[node_id].status = NodeStatus.ONLINE
            return self._nodes[node_id]

        node = WorkerNode(
            node_id=node_id,
            host=host,
            port=port,
            region=region,
            weight=weight,
        )
        self._nodes[node_id] = node
        logger.info("Worker registered: %s @ %s:%d [%s]", node_id, host, port, region)
        return node

    def deregister_node(self, node_id: str) -> None:
        if node_id in self._nodes:
            self._nodes[node_id].status = NodeStatus.OFFLINE
            del self._nodes[node_id]
            logger.info("Worker deregistered: %s", node_id)

    def heartbeat(self, node_id: str) -> bool:
        if node_id not in self._nodes:
            return False
        self._nodes[node_id].last_heartbeat = time.time()
        if self._nodes[node_id].status == NodeStatus.DEGRADED:
            self._nodes[node_id].status = NodeStatus.ONLINE
            logger.info("Node %s recovered", node_id)
        return True

    def drain_node(self, node_id: str) -> None:
        """Mark node for graceful shutdown — no new tasks dispatched."""
        if node_id in self._nodes:
            self._nodes[node_id].status = NodeStatus.DRAINING
            logger.info("Node %s set to DRAINING", node_id)

    # ------------------------------------------------------------------
    # Task dispatch
    # ------------------------------------------------------------------

    async def dispatch(self, payload: dict, max_attempts: int = 3) -> DistributedTask:
        task = DistributedTask(payload=payload, max_attempts=max_attempts)
        self._tasks[task.task_id] = task

        for attempt in range(max_attempts):
            task.attempt = attempt + 1
            try:
                node = self._lb.select(list(self._nodes.values()))
                task.assigned_node = node.node_id
                task.status = TaskStatus.DISPATCHED
                task.started_at = time.time()
                node.active_tasks += 1
                self._stats["dispatched"] += 1

                logger.debug(
                    "Task %s → node %s (attempt %d)",
                    task.task_id[:8],
                    node.node_id,
                    task.attempt,
                )

                # Simulate dispatch over internal RPC (real impl would be gRPC/HTTP)
                await self._rpc_dispatch(node, task)

                task.status = TaskStatus.COMPLETED
                task.completed_at = time.time()
                node.active_tasks = max(0, node.active_tasks - 1)
                node.total_tasks_handled += 1
                self._stats["completed"] += 1
                return task

            except NoHealthyWorkersError:
                task.status = TaskStatus.FAILED
                task.error = "No healthy workers"
                self._stats["failed"] += 1
                raise

            except Exception as exc:  # pylint: disable=broad-except
                node_id = task.assigned_node
                if node_id and node_id in self._nodes:
                    self._nodes[node_id].active_tasks = max(
                        0, self._nodes[node_id].active_tasks - 1
                    )
                    self._nodes[node_id].error_count += 1

                if attempt < max_attempts - 1:
                    task.status = TaskStatus.RETRYING
                    self._stats["retried"] += 1
                    backoff = 0.5 * (2**attempt)
                    logger.warning(
                        "Task %s failed (attempt %d): %s — retrying in %.1fs",
                        task.task_id[:8],
                        attempt + 1,
                        exc,
                        backoff,
                    )
                    await asyncio.sleep(backoff)
                else:
                    task.status = TaskStatus.FAILED
                    task.error = str(exc)
                    self._stats["failed"] += 1
                    raise TaskDispatchError(
                        f"Task {task.task_id[:8]} failed after {max_attempts} attempts"
                    ) from exc

        return task  # unreachable but satisfies type checker

    async def _rpc_dispatch(self, node: WorkerNode, task: DistributedTask) -> None:
        """
        Internal RPC stub. Replace with real gRPC/HTTP transport in production.
        Currently performs a no-op async yield to simulate network round-trip.
        """
        await asyncio.sleep(0)  # yield to event loop

    # ------------------------------------------------------------------
    # Heartbeat monitor
    # ------------------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        while self._running:
            await asyncio.sleep(HEARTBEAT_INTERVAL_S)
            now = time.time()
            for node in list(self._nodes.values()):
                age = now - node.last_heartbeat
                if age > NODE_TIMEOUT_S and node.status != NodeStatus.OFFLINE:
                    node.status = NodeStatus.OFFLINE
                    logger.warning(
                        "Node %s marked OFFLINE (no heartbeat for %.0fs)",
                        node.node_id,
                        age,
                    )
                elif age > NODE_TIMEOUT_S * 0.6 and node.status == NodeStatus.ONLINE:
                    node.status = NodeStatus.DEGRADED
                    logger.warning(
                        "Node %s DEGRADED (heartbeat age %.0fs)", node.node_id, age
                    )

    # ------------------------------------------------------------------
    # Stats / introspection
    # ------------------------------------------------------------------

    def get_cluster_stats(self) -> dict:
        nodes = list(self._nodes.values())
        healthy = sum(1 for n in nodes if n.is_healthy)
        return {
            "coordinator": f"{COORDINATOR_HOST}:{COORDINATOR_PORT}",
            "nodes": {
                "total": len(nodes),
                "healthy": healthy,
                "degraded": sum(1 for n in nodes if n.status == NodeStatus.DEGRADED),
                "offline": sum(1 for n in nodes if n.status == NodeStatus.OFFLINE),
                "draining": sum(1 for n in nodes if n.status == NodeStatus.DRAINING),
            },
            "tasks": {
                "total": len(self._tasks),
                "pending": sum(
                    1 for t in self._tasks.values() if t.status == TaskStatus.PENDING
                ),
                "running": sum(
                    1 for t in self._tasks.values() if t.status == TaskStatus.RUNNING
                ),
                "completed": self._stats["completed"],
                "failed": self._stats["failed"],
                "retried": self._stats["retried"],
            },
            "worker_details": [n.to_dict() for n in nodes],
        }

    def get_task(self, task_id: str) -> Optional[DistributedTask]:
        return self._tasks.get(task_id)


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------

_coordinator: Optional[DistributedCoordinator] = None


def get_coordinator() -> DistributedCoordinator:
    global _coordinator
    if _coordinator is None:
        _coordinator = DistributedCoordinator()
    return _coordinator
