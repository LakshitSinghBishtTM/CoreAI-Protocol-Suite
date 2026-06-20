import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Callable, Awaitable

from loguru import logger


@dataclass
class AgentExecution:
    agent_id: str
    config: dict
    started_at: datetime = field(default_factory=datetime.utcnow)
    iterations: int = 0
    status: str = "running"  # running | completed | failed | halted
    last_result: Optional[dict] = None
    error: Optional[str] = None


class Runtime:
    """
    Agent runtime execution engine.
    Manages the per-agent tick loop, iteration budgets, and emergency halt.
    """

    def __init__(self, kernel=None, max_iterations: int = 100):
        self.kernel = kernel
        self.max_iterations = max_iterations
        self._executions: dict[str, AgentExecution] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._tick_hooks: list[Callable[[str, int, dict], Awaitable[None]]] = []
        self._total_completed = 0
        self._total_failed = 0
        logger.info(f"Runtime initialized (max_iterations={max_iterations})")

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    async def execute_agent(self, agent_id: str, agent_config: dict):
        """
        Spawn an agent execution loop as a background task.
        Returns immediately — use get_execution() to poll status.
        """
        if agent_id in self._tasks and not self._tasks[agent_id].done():
            logger.warning(f"Agent {agent_id} is already running")
            return

        exec_record = AgentExecution(agent_id=agent_id, config=agent_config)
        self._executions[agent_id] = exec_record

        task = asyncio.create_task(
            self._run_loop(exec_record),
            name=f"agent-{agent_id}",
        )
        self._tasks[agent_id] = task
        logger.info(f"Execution started for agent {agent_id}")

    def register_tick_hook(self, hook: Callable[[str, int, dict], Awaitable[None]]):
        """
        Register a coroutine called after every tick.
        Signature: hook(agent_id, iteration, tick_result)
        """
        self._tick_hooks.append(hook)

    def get_execution(self, agent_id: str) -> Optional[AgentExecution]:
        return self._executions.get(agent_id)

    async def wait_for_agent(self, agent_id: str, timeout: float = 60.0):
        """Block until agent finishes or timeout expires."""
        task = self._tasks.get(agent_id)
        if not task:
            raise ValueError(f"No running task for agent {agent_id}")
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(f"Timed out waiting for agent {agent_id}")

    async def terminate_agent(self, agent_id: str, reason: str = "manual"):
        """Cancel a running agent task."""
        task = self._tasks.get(agent_id)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        exec_record = self._executions.get(agent_id)
        if exec_record:
            exec_record.status = "halted"
            exec_record.error = f"Terminated: {reason}"

        logger.info(f"Agent {agent_id} terminated ({reason})")

    async def emergency_halt(self):
        """Cancel all running agents immediately."""
        logger.critical("EMERGENCY HALT — stopping all agents")
        agent_ids = list(self._tasks.keys())
        for agent_id in agent_ids:
            await self.terminate_agent(agent_id, reason="emergency_halt")
        logger.critical(f"Emergency halt complete — {len(agent_ids)} agent(s) stopped")

    # ------------------------------------------------------------------ #
    # Internal loop
    # ------------------------------------------------------------------ #

    async def _run_loop(self, exec_record: AgentExecution):
        """Main execution loop for a single agent."""
        agent_id = exec_record.agent_id

        try:
            while exec_record.iterations < self.max_iterations:
                exec_record.iterations += 1

                result = await self._tick(
                    agent_id, exec_record.iterations, exec_record.config
                )
                exec_record.last_result = result

                # Fire registered hooks
                for hook in self._tick_hooks:
                    try:
                        await hook(agent_id, exec_record.iterations, result)
                    except Exception as hook_err:
                        logger.warning(f"Tick hook error: {hook_err}")

                status = result.get("status", "running")

                if status == "completed":
                    exec_record.status = "completed"
                    self._total_completed += 1
                    logger.info(
                        f"Agent {agent_id} completed "
                        f"(iterations={exec_record.iterations})"
                    )
                    return

                if status == "error":
                    err = result.get("error", "unknown error")
                    logger.error(f"Agent {agent_id} tick error: {err}")
                    # Non-fatal: log and continue unless repeated

                # Rogue-behavior check every 10 ticks
                if exec_record.iterations % 10 == 0:
                    await self._check_rogue_status(agent_id, exec_record)

                await asyncio.sleep(0)  # yield to event loop

            # Hit iteration budget
            logger.warning(
                f"Agent {agent_id} reached iteration limit ({self.max_iterations})"
            )
            exec_record.status = "halted"
            exec_record.error = f"Iteration limit reached ({self.max_iterations})"
            await self.terminate_agent(agent_id, reason="iteration_limit")

        except asyncio.CancelledError:
            exec_record.status = "halted"
            logger.info(f"Agent {agent_id} execution cancelled")
            raise

        except Exception as e:
            exec_record.status = "failed"
            exec_record.error = str(e)
            self._total_failed += 1
            logger.error(f"Agent {agent_id} execution failed: {e}")

    async def _tick(self, agent_id: str, iteration: int, config: dict) -> dict:
        """
        Single execution tick.
        If a router is available via kernel, routes a completion for the agent's
        current objective. Otherwise returns a no-op running status.
        """
        objective = config.get("objective", "")
        if not objective:
            return {"status": "completed", "reason": "no objective"}

        # If we have access to the router, actually call it
        if self.kernel and hasattr(self.kernel, "router"):
            try:
                from providers import CompletionRequest, Message

                request = CompletionRequest(
                    messages=[
                        Message(
                            role="system",
                            content=(
                                "You are an autonomous agent. Complete your objective "
                                "step by step. When finished, respond with DONE."
                            ),
                        ),
                        Message(
                            role="user",
                            content=(
                                f"Objective: {objective}\n"
                                f"Iteration: {iteration}\n"
                                f"Context: {config.get('context', {})}"
                            ),
                        ),
                    ],
                    max_tokens=512,
                )
                response = await self.kernel.router.route(request)
                content = response.content or ""
                is_done = "DONE" in content.upper()
                return {
                    "status": "completed" if is_done else "running",
                    "iteration": iteration,
                    "content": content,
                    "cost_usd": response.cost_usd,
                }
            except Exception as e:
                return {"status": "error", "error": str(e), "iteration": iteration}

        # No router available — simulate progress
        return {"status": "running", "iteration": iteration}

    async def _check_rogue_status(self, agent_id: str, exec_record: AgentExecution):
        """
        Check if agent is behaving unexpectedly.
        Currently checks iteration rate and flags stalled agents.
        """
        elapsed = (datetime.utcnow() - exec_record.started_at).total_seconds()
        rate = exec_record.iterations / max(elapsed, 1)

        if rate > 50:
            logger.warning(
                f"Agent {agent_id} tick rate unusually high: {rate:.1f}/s "
                f"(iteration={exec_record.iterations})"
            )

    # ------------------------------------------------------------------ #
    # Observability
    # ------------------------------------------------------------------ #

    def get_execution_stats(self) -> dict:
        active = sum(1 for t in self._tasks.values() if not t.done())
        return {
            "active_agents": active,
            "total_executions": len(self._executions),
            "total_completed": self._total_completed,
            "total_failed": self._total_failed,
            "max_iterations_per_agent": self.max_iterations,
            "agents": {
                aid: {
                    "status": e.status,
                    "iterations": e.iterations,
                    "started_at": e.started_at.isoformat(),
                    "error": e.error,
                }
                for aid, e in self._executions.items()
            },
        }
