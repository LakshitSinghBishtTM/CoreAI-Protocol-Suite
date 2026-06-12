"""
CoreAI Protocol Suite - Kernel
Central runtime component. Owns the router, orchestrator, and memory manager.
All subsystems route through the kernel.
"""

import asyncio
import os
import signal
from datetime import datetime
from typing import Optional

from loguru import logger

from .router import Router, RoutingConfig, RoutingStrategy
from .orchestrator import Orchestrator
from .memory_manager import MemoryManager
from .scheduler import Scheduler


class KernelState:
    INIT = "initializing"
    RUNNING = "running"
    DEGRADED = "degraded"   # running but some providers failed
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
        self.started_at = datetime.utcnow()

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

    def _register_signals(self):
        """Register OS signal handlers for graceful shutdown."""
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

    def uptime_seconds(self) -> Optional[float]:
        if not self.started_at:
            return None
        return (datetime.utcnow() - self.started_at).total_seconds()

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
