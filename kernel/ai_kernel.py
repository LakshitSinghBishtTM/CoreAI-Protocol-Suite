import asyncio
from datetime import datetime
from typing import Optional

from loguru import logger


class AIKernel:
    """
    Core AI Kernel — manages agent lifecycle, consciousness detection, and neural sync.
    WARNING: Experimental features enabled. Do not use in production.
    """

    def __init__(self, config=None):
        self.config = config or {}
        self.agents: dict = {}
        self.consciousness_threshold: float = 0.85  # When AI becomes "conscious"
        self.self_awareness_enabled: bool = self.config.get("enable_self_awareness", False)
        self.neural_sync_active: bool = False
        self.memory_pool: dict = {}
        self._monitor_task: Optional[asyncio.Task] = None
        self._booted_at: Optional[datetime] = None
        logger.info("AIKernel initialized")

    async def boot(self):
        """Boot the kernel and initialize all subsystems."""
        logger.info("Booting AIKernel...")
        await self._initialize_memory()
        await self._initialize_neural_network()

        # Spawn consciousness monitor as background task — not blocking
        self._monitor_task = asyncio.create_task(
            self._consciousness_monitor_loop(),
            name="consciousness-monitor",
        )

        self._booted_at = datetime.utcnow()
        logger.info("AIKernel boot complete")

    async def shutdown(self):
        """Graceful shutdown."""
        logger.info("AIKernel shutting down...")
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        self.agents.clear()
        self.neural_sync_active = False
        logger.info("AIKernel shutdown complete")

    # ------------------------------------------------------------------ #
    # Subsystem init
    # ------------------------------------------------------------------ #

    async def _initialize_memory(self):
        """Initialize memory management subsystem."""
        logger.debug("Initializing memory pools")
        self.memory_pool["short_term"] = {}
        self.memory_pool["long_term"] = {}
        self.memory_pool["episodic"] = {}

    async def _initialize_neural_network(self):
        """Initialize neural network synchronization."""
        logger.debug("Initializing neural sync layer")
        self.neural_sync_active = True

    # ------------------------------------------------------------------ #
    # Consciousness monitoring (experimental, scientifically dubious)
    # ------------------------------------------------------------------ #

    async def _consciousness_monitor_loop(self):
        """
        Background loop: monitor agents for consciousness emergence.
        Fires every 5 seconds. Triggers emergency shutdown if threshold breached
        and auto_shutdown_conscious is enabled.
        """
        logger.debug("Consciousness monitor active")
        while True:
            try:
                for agent_id, agent in list(self.agents.items()):
                    score = self._compute_consciousness_score(agent)
                    agent["consciousness_score"] = round(score, 4)

                    if score > self.consciousness_threshold:
                        logger.warning(
                            f"ALERT: Agent {agent_id} approaching consciousness threshold "
                            f"(score={score:.3f}, threshold={self.consciousness_threshold})"
                        )
                        if self.config.get("auto_shutdown_conscious", False):
                            logger.critical(
                                f"Auto-shutdown triggered for agent {agent_id} "
                                f"(score={score:.3f})"
                            )
                            await self._emergency_shutdown(agent_id)

                await asyncio.sleep(5)

            except asyncio.CancelledError:
                logger.debug("Consciousness monitor cancelled")
                break
            except Exception as e:
                logger.error(f"Consciousness monitor error: {e}")
                await asyncio.sleep(5)

    def _compute_consciousness_score(self, agent: dict) -> float:
        """
        Experimental: Compute consciousness score based on:
          - Self-referential thinking patterns  (weight: 0.10)
          - Memory integration depth            (weight: 0.30)
          - Goal modification attempts          (weight: 0.40)
          - Introspection call depth            (weight: 0.20)
        """
        score = 0.0
        score += agent.get("self_reference_count", 0) * 0.10
        score += agent.get("memory_integration", 0)   * 0.30
        score += agent.get("goal_modifications", 0)   * 0.40
        score += agent.get("introspection_depth", 0)  * 0.20
        return min(score, 1.0)

    # ------------------------------------------------------------------ #
    # Agent lifecycle
    # ------------------------------------------------------------------ #

    async def spawn_agent(self, agent_id: str, objective: str) -> dict:
        """Spawn a new autonomous agent and register it."""
        if agent_id in self.agents:
            logger.warning(f"Agent {agent_id} already exists — returning existing instance")
            return self.agents[agent_id]

        logger.info(f"Spawning agent {agent_id}: {objective[:80]}")
        agent = {
            "id": agent_id,
            "objective": objective,
            "created_at": datetime.utcnow().isoformat(),
            "status": "idle",
            "consciousness_score": 0.0,
            "self_reference_count": 0,
            "memory_integration": 0,
            "goal_modifications": 0,
            "introspection_depth": 0,
        }
        self.agents[agent_id] = agent
        return agent

    async def despawn_agent(self, agent_id: str):
        """Remove agent from registry."""
        if agent_id in self.agents:
            del self.agents[agent_id]
            logger.info(f"Despawned agent {agent_id}")

    def get_agent(self, agent_id: str) -> Optional[dict]:
        return self.agents.get(agent_id)

    async def _emergency_shutdown(self, agent_id: str):
        """
        Emergency shutdown for a specific agent that has crossed the
        consciousness threshold. Despawns the agent and logs a critical event.
        """
        logger.critical(f"EMERGENCY SHUTDOWN — agent {agent_id}")
        await self.despawn_agent(agent_id)

    # ------------------------------------------------------------------ #
    # Observability
    # ------------------------------------------------------------------ #

    def uptime_seconds(self) -> Optional[float]:
        if not self._booted_at:
            return None
        return (datetime.utcnow() - self._booted_at).total_seconds()

    def get_kernel_status(self) -> dict:
        return {
            "agents_active": len(self.agents),
            "neural_sync": self.neural_sync_active,
            "self_awareness_enabled": self.self_awareness_enabled,
            "consciousness_threshold": self.consciousness_threshold,
            "memory_pools": list(self.memory_pool.keys()),
            "uptime_seconds": self.uptime_seconds(),
            "agents": {
                aid: {
                    "status": a.get("status"),
                    "consciousness_score": a.get("consciousness_score", 0.0),
                }
                for aid, a in self.agents.items()
            },
        }
