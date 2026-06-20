"""
agents/agent_manager.py

Central registry and lifecycle manager for all CoreAI agents.
Handles agent creation, health monitoring, and teardown.
"""

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from coreai.kernel import Kernel
from coreai.memory_manager import MemoryManager
from agents.autonomous_agent import AutonomousAgent
from agents.task_orchestrator import TaskOrchestrator

logger = logging.getLogger(__name__)


class AgentStatus:
    INITIALIZING = "initializing"
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    FAILED = "failed"
    TERMINATED = "terminated"


class AgentRecord:
    def __init__(self, agent_id: str, agent: AutonomousAgent, metadata: Dict):
        self.agent_id = agent_id
        self.agent = agent
        self.metadata = metadata
        self.status = AgentStatus.INITIALIZING
        self.created_at = datetime.now(timezone.utc)
        self.last_active = datetime.now(timezone.utc)
        self.task_count = 0
        self.error_count = 0

    def to_dict(self) -> Dict:
        return {
            "agent_id": self.agent_id,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "last_active": self.last_active.isoformat(),
            "task_count": self.task_count,
            "error_count": self.error_count,
            "metadata": self.metadata,
        }


class AgentManager:
    """
    Manages the full lifecycle of CoreAI autonomous agents.
    Supports creation, health checks, dynamic scaling, and graceful shutdown.
    """

    MAX_AGENTS = 32
    HEALTH_CHECK_INTERVAL = 30  # seconds

    def __init__(self, kernel: Kernel, memory_manager: MemoryManager):
        self.kernel = kernel
        self.memory = memory_manager
        self.agents: Dict[str, AgentRecord] = {}
        self.orchestrator = TaskOrchestrator(self)
        self._health_task: Optional[asyncio.Task] = None
        self._shutdown_event = asyncio.Event()
        logger.info("AgentManager initialized")

    # ------------------------------------------------------------------
    # Agent lifecycle
    # ------------------------------------------------------------------

    async def spawn_agent(
        self,
        name: str,
        capabilities: List[str],
        config: Optional[Dict] = None,
    ) -> str:
        if len(self.agents) >= self.MAX_AGENTS:
            raise RuntimeError(
                f"Agent limit reached ({self.MAX_AGENTS}). "
                "Terminate idle agents before spawning new ones."
            )

        agent_id = f"agent-{uuid.uuid4().hex[:8]}"
        config = config or {}

        agent = AutonomousAgent(
            agent_id=agent_id,
            name=name,
            capabilities=capabilities,
            kernel=self.kernel,
            memory=self.memory,
            config=config,
        )

        record = AgentRecord(
            agent_id=agent_id,
            agent=agent,
            metadata={"name": name, "capabilities": capabilities},
        )
        self.agents[agent_id] = record

        try:
            await agent.initialize()
            record.status = AgentStatus.IDLE
            logger.info("Spawned agent %s (%s)", agent_id, name)
        except Exception as exc:
            record.status = AgentStatus.FAILED
            logger.error("Failed to initialize agent %s: %s", agent_id, exc)
            raise

        return agent_id

    async def terminate_agent(self, agent_id: str, reason: str = "manual") -> None:
        record = self._get_record(agent_id)
        logger.info("Terminating agent %s (reason: %s)", agent_id, reason)

        try:
            await record.agent.shutdown()
        except Exception as exc:
            logger.warning("Error during agent %s shutdown: %s", agent_id, exc)
        finally:
            record.status = AgentStatus.TERMINATED
            del self.agents[agent_id]

    async def pause_agent(self, agent_id: str) -> None:
        record = self._get_record(agent_id)
        await record.agent.pause()
        record.status = AgentStatus.PAUSED
        logger.info("Paused agent %s", agent_id)

    async def resume_agent(self, agent_id: str) -> None:
        record = self._get_record(agent_id)
        await record.agent.resume()
        record.status = AgentStatus.IDLE
        logger.info("Resumed agent %s", agent_id)

    # ------------------------------------------------------------------
    # Task dispatch
    # ------------------------------------------------------------------

    async def dispatch_task(self, agent_id: str, task: Dict) -> Any:
        record = self._get_record(agent_id)

        if record.status not in (AgentStatus.IDLE, AgentStatus.RUNNING):
            raise RuntimeError(
                f"Agent {agent_id} is not available (status: {record.status})"
            )

        record.status = AgentStatus.RUNNING
        record.last_active = datetime.now(timezone.utc)
        record.task_count += 1

        try:
            result = await record.agent.execute(task)
            record.status = AgentStatus.IDLE
            return result
        except Exception as exc:
            record.error_count += 1
            record.status = AgentStatus.FAILED
            logger.error("Agent %s task failed: %s", agent_id, exc)
            raise

    # ------------------------------------------------------------------
    # Health monitoring
    # ------------------------------------------------------------------

    async def start_health_monitor(self) -> None:
        self._health_task = asyncio.create_task(self._health_loop())
        logger.info(
            "Health monitor started (interval: %ds)", self.HEALTH_CHECK_INTERVAL
        )

    async def _health_loop(self) -> None:
        while not self._shutdown_event.is_set():
            await self._check_all_agents()
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=self.HEALTH_CHECK_INTERVAL,
                )
            except asyncio.TimeoutError:
                pass

    async def _check_all_agents(self) -> None:
        for agent_id, record in list(self.agents.items()):
            try:
                healthy = await record.agent.health_check()
                if not healthy:
                    logger.warning(
                        "Agent %s failed health check — restarting", agent_id
                    )
                    await self._restart_agent(agent_id)
            except Exception as exc:
                logger.error("Health check error for agent %s: %s", agent_id, exc)

    async def _restart_agent(self, agent_id: str) -> None:
        record = self.agents.get(agent_id)
        if not record:
            return
        try:
            await record.agent.shutdown()
        except Exception:
            pass
        record.status = AgentStatus.INITIALIZING
        record.error_count = 0
        await record.agent.initialize()
        record.status = AgentStatus.IDLE
        logger.info("Agent %s restarted", agent_id)

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def list_agents(self) -> List[Dict]:
        return [r.to_dict() for r in self.agents.values()]

    def get_agent_status(self, agent_id: str) -> Dict:
        return self._get_record(agent_id).to_dict()

    def get_idle_agents(self) -> List[str]:
        return [aid for aid, r in self.agents.items() if r.status == AgentStatus.IDLE]

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def shutdown_all(self) -> None:
        logger.info("Shutting down all agents (%d total)", len(self.agents))
        self._shutdown_event.set()

        if self._health_task:
            self._health_task.cancel()

        await asyncio.gather(
            *[
                self.terminate_agent(aid, reason="system_shutdown")
                for aid in list(self.agents.keys())
            ],
            return_exceptions=True,
        )
        logger.info("All agents terminated")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_record(self, agent_id: str) -> AgentRecord:
        record = self.agents.get(agent_id)
        if not record:
            raise KeyError(f"No agent with ID: {agent_id}")
        return record
