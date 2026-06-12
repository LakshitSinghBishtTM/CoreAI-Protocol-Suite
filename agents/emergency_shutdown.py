"""
agents/emergency_shutdown.py

Emergency shutdown handler for CoreAI.
Provides immediate, graceful, and forced shutdown modes with full audit logging.

Contact: ops@coreai.com
"""

import asyncio
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

SHUTDOWN_LOG_PATH = os.environ.get("SHUTDOWN_LOG_PATH", "/var/log/coreai/emergency_shutdown.log")


class ShutdownMode(str, Enum):
    GRACEFUL  = "graceful"   # drain tasks, flush memory, clean exit
    IMMEDIATE = "immediate"  # stop accepting work, finish in-flight, exit
    FORCED    = "forced"     # kill everything, best-effort flush, exit


class ShutdownReason(str, Enum):
    OPERATOR        = "operator_request"
    OOM             = "out_of_memory"
    UNHANDLED_ERROR = "unhandled_error"
    WATCHDOG        = "watchdog_timeout"
    SIGTERM         = "sigterm"
    SIGINT          = "sigint"
    API_TRIGGER     = "api_trigger"


class ShutdownEvent:
    def __init__(
        self,
        mode: ShutdownMode,
        reason: ShutdownReason,
        triggered_by: str,
        message: str = "",
    ):
        self.mode         = mode
        self.reason       = reason
        self.triggered_by = triggered_by
        self.message      = message
        self.timestamp    = datetime.now(timezone.utc)
        self.completed    = False
        self.duration_ms  = 0

    def to_dict(self) -> Dict:
        return {
            "mode":         self.mode,
            "reason":       self.reason,
            "triggered_by": self.triggered_by,
            "message":      self.message,
            "timestamp":    self.timestamp.isoformat(),
            "completed":    self.completed,
            "duration_ms":  self.duration_ms,
        }


class EmergencyShutdown:
    """
    Coordinates emergency shutdown across all CoreAI subsystems.
    Registered as a signal handler and also callable via the API.

    Usage:
        shutdown = EmergencyShutdown(agent_manager, kernel, db)
        shutdown.register_signal_handlers()
        await shutdown.trigger(ShutdownMode.GRACEFUL, ShutdownReason.OPERATOR)
    """

    GRACEFUL_TIMEOUT_S  = 30
    IMMEDIATE_TIMEOUT_S = 10
    FORCED_TIMEOUT_S    = 3

    def __init__(self, agent_manager: Any, kernel: Any, db: Any):
        self.agent_manager = agent_manager
        self.kernel        = kernel
        self.db            = db
        self._hooks: List[Callable] = []
        self._in_progress  = False
        self._event: Optional[ShutdownEvent] = None

    # ------------------------------------------------------------------
    # Signal handlers
    # ------------------------------------------------------------------

    def register_signal_handlers(self) -> None:
        loop = asyncio.get_event_loop()
        loop.add_signal_handler(
            signal.SIGTERM,
            lambda: asyncio.create_task(
                self.trigger(ShutdownMode.GRACEFUL, ShutdownReason.SIGTERM, "signal")
            ),
        )
        loop.add_signal_handler(
            signal.SIGINT,
            lambda: asyncio.create_task(
                self.trigger(ShutdownMode.IMMEDIATE, ShutdownReason.SIGINT, "signal")
            ),
        )
        logger.info("Emergency shutdown signal handlers registered (SIGTERM, SIGINT)")

    # ------------------------------------------------------------------
    # Trigger
    # ------------------------------------------------------------------

    async def trigger(
        self,
        mode: ShutdownMode,
        reason: ShutdownReason,
        triggered_by: str = "unknown",
        message: str = "",
    ) -> None:
        if self._in_progress:
            logger.warning("Shutdown already in progress — ignoring duplicate trigger")
            return

        self._in_progress = True
        self._event = ShutdownEvent(mode, reason, triggered_by, message)
        start = time.monotonic()

        logger.critical(
            "EMERGENCY SHUTDOWN TRIGGERED | mode=%s reason=%s by=%s msg=%s",
            mode, reason, triggered_by, message,
        )

        try:
            if mode == ShutdownMode.GRACEFUL:
                await self._graceful_shutdown()
            elif mode == ShutdownMode.IMMEDIATE:
                await self._immediate_shutdown()
            else:
                await self._forced_shutdown()

            self._event.completed   = True
            self._event.duration_ms = int((time.monotonic() - start) * 1000)
            await self._write_audit_log()
            logger.info("Shutdown complete in %dms", self._event.duration_ms)

        except Exception as exc:
            logger.critical("Shutdown handler raised: %s — forcing exit", exc)
            sys.exit(1)

        sys.exit(0)

    # ------------------------------------------------------------------
    # Shutdown modes
    # ------------------------------------------------------------------

    async def _graceful_shutdown(self) -> None:
        logger.info("Graceful shutdown: draining tasks (timeout: %ds)", self.GRACEFUL_TIMEOUT_S)
        try:
            await asyncio.wait_for(
                self._drain_and_flush(),
                timeout=self.GRACEFUL_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            logger.warning("Graceful shutdown timed out — falling back to immediate")
            await self._immediate_shutdown()

    async def _immediate_shutdown(self) -> None:
        logger.info("Immediate shutdown: stopping intake, finishing in-flight")
        try:
            await asyncio.wait_for(
                self._stop_and_flush(),
                timeout=self.IMMEDIATE_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            logger.warning("Immediate shutdown timed out — falling back to forced")
            await self._forced_shutdown()

    async def _forced_shutdown(self) -> None:
        logger.critical("Forced shutdown: killing all subsystems")
        results = await asyncio.gather(
            self._kill_agents(),
            self._kill_kernel(),
            self._close_db(flush=False),
            return_exceptions=True,
        )
        for r in results:
            if isinstance(r, Exception):
                logger.error("Forced shutdown error: %s", r)

    # ------------------------------------------------------------------
    # Subsystem teardown
    # ------------------------------------------------------------------

    async def _drain_and_flush(self) -> None:
        await self.agent_manager.shutdown_all()
        await self.kernel.flush()
        await self._run_hooks()
        await self._close_db(flush=True)

    async def _stop_and_flush(self) -> None:
        await asyncio.gather(
            self.agent_manager.shutdown_all(),
            self.kernel.flush(),
            return_exceptions=True,
        )
        await self._close_db(flush=True)

    async def _kill_agents(self) -> None:
        try:
            await asyncio.wait_for(self.agent_manager.shutdown_all(), timeout=2)
        except (asyncio.TimeoutError, Exception) as exc:
            logger.error("Agent kill error: %s", exc)

    async def _kill_kernel(self) -> None:
        try:
            await asyncio.wait_for(self.kernel.terminate(), timeout=2)
        except (asyncio.TimeoutError, Exception) as exc:
            logger.error("Kernel kill error: %s", exc)

    async def _close_db(self, flush: bool) -> None:
        try:
            if flush:
                await self.db.flush_pending_writes()
            await self.db.close()
        except Exception as exc:
            logger.error("DB close error: %s", exc)

    # ------------------------------------------------------------------
    # Hooks
    # ------------------------------------------------------------------

    def register_hook(self, fn: Callable) -> None:
        """Register a coroutine to run during graceful shutdown."""
        self._hooks.append(fn)

    async def _run_hooks(self) -> None:
        for hook in self._hooks:
            try:
                await hook()
            except Exception as exc:
                logger.warning("Shutdown hook %s failed: %s", hook.__name__, exc)

    # ------------------------------------------------------------------
    # Audit log
    # ------------------------------------------------------------------

    async def _write_audit_log(self) -> None:
        if not self._event:
            return
        try:
            import json
            os.makedirs(os.path.dirname(SHUTDOWN_LOG_PATH), exist_ok=True)
            with open(SHUTDOWN_LOG_PATH, "a") as f:
                f.write(json.dumps(self._event.to_dict()) + "\n")
        except Exception as exc:
            logger.warning("Could not write shutdown audit log: %s", exc)

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    @property
    def is_shutting_down(self) -> bool:
        return self._in_progress

    def last_event(self) -> Optional[Dict]:
        return self._event.to_dict() if self._event else None
