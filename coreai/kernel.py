"""
CoreAI Protocol Suite - Kernel
Central runtime component. Owns the router, orchestrator, and memory manager.
All subsystems route through the kernel.
"""

import asyncio
import signal
from datetime import datetime, timezone
from typing import Optional

from loguru import logger

from .memory_manager import MemoryManager
from .orchestrator import Orchestrator
from .router import Router
from .scheduler import Scheduler


class AgentCompletionResult:
    """Return type of Kernel.complete(); shape AutonomousAgent.execute() reads."""

    def __init__(
        self, content: str, is_final: bool = True, tool_call: Optional[dict] = None
    ):
        self.content = content
        self.is_final = is_final
        self.tool_call = tool_call


class KernelState:
    INIT = "initializing"
    RUNNING = "running"
    DEGRADED = "degraded"  # running but some providers failed
    STOPPING = "stopping"
    STOPPED = "stopped"


class Kernel:
    """
    Central runtime kernel.
    Manages component lifecycle, shutdown, and exposes the unified system interface.
    """

    def __init__(
        self,
        router: Router,
        orchestrator: Orchestrator,
        memory_manager: Optional[MemoryManager] = None,
        scheduler: Optional[Scheduler] = None,
    ):
        self.router = router
        self.orchestrator = orchestrator
        self.memory = memory_manager or MemoryManager()
        self.scheduler = scheduler or Scheduler()

        self.state = KernelState.INIT
        self.started_at: Optional[datetime] = None
        self._shutdown_event = asyncio.Event()

        logger.info("Kernel created")

    async def start(self):
        """Start all subsystems."""
        logger.info("Kernel starting subsystems...")

        await self.memory.start()
        logger.debug("  MemoryManager — up")

        await self.scheduler.start()
        logger.debug("  Scheduler — up")

        self.state = KernelState.RUNNING
        self.started_at = datetime.now(timezone.utc)

        self._register_signals()
        logger.info(f"Kernel running (providers: {list(self.router.providers)})")

    async def stop(self):
        """Graceful shutdown — drain in-flight requests then stop subsystems."""
        if self.state == KernelState.STOPPED:
            return

        self.state = KernelState.STOPPING
        logger.info("Kernel shutting down...")

        # Cancel pending agent tasks
        pending = self.orchestrator.get_pending_tasks()
        for task in pending:
            self.orchestrator.cancel_task(task.task_id)
        if pending:
            logger.info(f"  Cancelled {len(pending)} pending task(s)")

        await self.scheduler.stop()
        logger.debug("  Scheduler — down")

        await self.memory.stop()
        logger.debug("  MemoryManager — down")

        self.state = KernelState.STOPPED
        self._shutdown_event.set()
        logger.info("Kernel stopped")

    async def wait_for_shutdown(self):
        """Block until shutdown is triggered."""
        await self._shutdown_event.wait()

    async def flush(self, timeout_s: float = 5.0) -> None:
        """
        Give any RUNNING orchestrator tasks a bounded window to finish
        naturally, before stop() cancels whatever's left.

        Distinct from stop(): this waits for work to complete, stop()
        cancels it. Used by EmergencyShutdown's graceful/immediate
        modes, which call flush() before stop().
        """
        deadline = asyncio.get_event_loop().time() + timeout_s
        while self.orchestrator.get_active_tasks():
            if asyncio.get_event_loop().time() >= deadline:
                remaining = len(self.orchestrator.get_active_tasks())
                logger.warning(
                    f"Kernel flush timed out after {timeout_s}s with "
                    f"{remaining} task(s) still running"
                )
                return
            await asyncio.sleep(0.1)
        logger.debug("Kernel flush: no active tasks remaining")

    async def terminate(self) -> None:
        """
        Forced-mode kernel teardown, called by EmergencyShutdown's
        FORCED mode. There's no lower-level resource here (no OS
        process, no thread pool) to kill more forcibly than stop()
        already tears down -- so this reuses that teardown. Kept as
        its own method, rather than having EmergencyShutdown call
        stop() directly, so forced-mode semantics can diverge later
        without changing the shutdown coordinator.
        """
        await self.stop()

    def _register_signals(self):
        """Register OS signal handlers for graceful shutdown.

        Best-effort: only the main thread of the main interpreter can
        register signal handlers. Test runners (e.g. FastAPI's
        TestClient, which drives the ASGI app's lifespan from a worker
        thread via anyio) and some embedding contexts can't do this --
        that's expected there, not a startup failure. A real uvicorn
        deployment runs in the main thread, where this still works.
        """
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(
                    sig,
                    lambda: asyncio.create_task(self.stop()),
                )
            except NotImplementedError:
                # Windows doesn't support add_signal_handler
                pass
            except RuntimeError:
                # Not the main thread of the main interpreter
                logger.debug(
                    "Skipping signal handler registration for %s "
                    "(not in main thread)",
                    sig,
                )

    def uptime_seconds(self) -> Optional[float]:
        if not self.started_at:
            return None
        return (datetime.now(timezone.utc) - self.started_at).total_seconds()

    def health(self) -> dict:
        return {
            "state": self.state,
            "uptime_seconds": self.uptime_seconds(),
            "providers": list(self.router.providers.keys()),
            "active_tasks": len(self.orchestrator.get_active_tasks()),
            "memory": self.memory.stats(),
            "scheduler": self.scheduler.stats(),
        }

    def stats(self) -> dict:
        return {
            "kernel": self.health(),
            "router": self.router.stats(),
            "orchestrator": self.orchestrator.stats(),
        }

    # ------------------------------------------------------------------
    # Agent-facing API
    #
    # agents/autonomous_agent.py calls warm_up/ping/complete on whatever
    # Kernel it's given. None of these existed anywhere before this fix --
    # every agent's initialize()/health_check()/execute() would have
    # raised AttributeError the moment it ran, and nothing caught this
    # because agents/ has no tests. Adding the real, minimal versions
    # rather than stubs: warm_up/ping are cheap liveness/readiness
    # checks, complete() is a genuine adapter onto the same Router
    # every other request goes through (not a second code path).
    # ------------------------------------------------------------------

    async def warm_up(self, agent_id: str) -> None:
        """Readiness hook called once when an agent initializes.

        No per-agent state to pre-load today (agents don't have a
        dedicated cache slot yet) -- this just confirms the kernel is
        actually running before the agent starts using it, so a bad
        startup ordering fails at initialize() with a clear error
        rather than later, mid-task.
        """
        if self.state != KernelState.RUNNING:
            raise RuntimeError(
                f"Cannot warm up agent {agent_id}: kernel state is "
                f"'{self.state}', not '{KernelState.RUNNING}'"
            )
        logger.debug(f"Kernel warm-up ok for agent {agent_id}")

    async def ping(self, agent_id: str) -> bool:
        """Liveness check used by AutonomousAgent.health_check()."""
        return self.state == KernelState.RUNNING

    async def complete(
        self,
        agent_id: str,
        messages: list[dict],
        tools: Optional[list[str]] = None,
    ) -> "AgentCompletionResult":
        """
        Run one completion for an agent's tick loop, through the same
        Router (and therefore the same cache/limiter/retry/cost
        tracking) as every other request in the system.

        tools is accepted for interface compatibility with a future
        real tool-calling protocol, but no provider adapter in this
        codebase currently returns a tool-call shape back -- so every
        result here is final. That's why is_final is always True and
        tool_call is always None, not a guess: there is nothing yet
        upstream that could produce anything else.
        """
        from providers import CompletionRequest, Message

        system_prompt = next(
            (m["content"] for m in messages if m.get("role") == "system"), None
        )
        chat_messages = [
            Message(role=m["role"], content=m["content"])
            for m in messages
            if m.get("role") != "system"
        ]
        request = CompletionRequest(
            messages=chat_messages,
            system_prompt=system_prompt,
        )
        response = await self.router.route(request)
        return AgentCompletionResult(content=response.content)


# ------------------------------------------------------------------
# Module-level singleton
#
# Deliberately NOT self-constructing a Router/Orchestrator here: the
# app already owns those instances (api/server.py's app_state). init_kernel
# is called once at startup with the app's existing instances so there is
# only ever one Router/Orchestrator pair, not two disconnected ones.
# ------------------------------------------------------------------

_kernel_instance: Optional["Kernel"] = None


def init_kernel(router: Router, orchestrator: Orchestrator, **kwargs) -> "Kernel":
    global _kernel_instance
    _kernel_instance = Kernel(router, orchestrator, **kwargs)
    return _kernel_instance


def get_kernel() -> "Kernel":
    if _kernel_instance is None:
        raise RuntimeError(
            "Kernel not initialized — call init_kernel() during app startup first"
        )
    return _kernel_instance
