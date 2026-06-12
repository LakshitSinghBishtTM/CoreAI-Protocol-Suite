"""
runtime/engine.py

CoreAI Runtime Engine — top-level orchestration entry point for the
request processing pipeline. Wires together the router, cache, rate
limiter, GPU batch processor, and distributed coordinator into a single
cohesive execution context.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator, Optional

from runtime.distributed_runtime import DistributedCoordinator, get_coordinator
from runtime.gpu_acceleration import GPUAccelerationConfig, get_batch_processor

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ENGINE_VERSION = "1.4.2"
DEFAULT_REQUEST_TIMEOUT_S = float(os.environ.get("COREAI_REQUEST_TIMEOUT", "30.0"))
MAX_CONCURRENT_REQUESTS = int(os.environ.get("COREAI_MAX_CONCURRENT", "128"))
ENABLE_DISTRIBUTED = os.environ.get("COREAI_DISTRIBUTED", "false").lower() == "true"
ENABLE_GPU = os.environ.get("COREAI_GPU_ENABLED", "false").lower() == "true"
ENGINE_ID = os.environ.get("COREAI_ENGINE_ID", str(uuid.uuid4())[:8])


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class EngineState(str, Enum):
    COLD = "cold"
    INITIALIZING = "initializing"
    READY = "ready"
    DEGRADED = "degraded"
    SHUTTING_DOWN = "shutting_down"
    STOPPED = "stopped"


class ProcessingMode(str, Enum):
    SYNC = "sync"
    ASYNC = "async"
    STREAMING = "streaming"
    BATCH = "batch"


# ---------------------------------------------------------------------------
# Request / Response context
# ---------------------------------------------------------------------------


@dataclass
class EngineRequest:
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    payload: dict = field(default_factory=dict)
    mode: ProcessingMode = ProcessingMode.ASYNC
    timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S
    priority: int = 5  # 1 (highest) – 10 (lowest)
    trace_id: Optional[str] = None
    metadata: dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)


@dataclass
class EngineResponse:
    request_id: str
    result: Any
    engine_id: str = ENGINE_ID
    latency_ms: float = 0.0
    cached: bool = False
    gpu_accelerated: bool = False
    distributed: bool = False
    error: Optional[str] = None
    metadata: dict = field(default_factory=dict)

    @property
    def success(self) -> bool:
        return self.error is None


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class EngineNotReadyError(RuntimeError):
    """Raised when a request arrives before the engine is fully initialised."""


class EngineShutdownError(RuntimeError):
    """Raised when a request arrives during engine shutdown."""


class RequestTimeoutError(asyncio.TimeoutError):
    """Raised when a request exceeds its configured timeout."""


# ---------------------------------------------------------------------------
# Pipeline stages
# ---------------------------------------------------------------------------


class PipelineStage:
    """Base class for engine processing stages."""

    name: str = "base"

    async def process(self, request: EngineRequest) -> EngineRequest:
        return request

    async def teardown(self) -> None:
        pass


class ValidationStage(PipelineStage):
    name = "validation"

    _REQUIRED_FIELDS = {"messages"}

    async def process(self, request: EngineRequest) -> EngineRequest:
        missing = self._REQUIRED_FIELDS - set(request.payload.keys())
        if missing:
            raise ValueError(f"Request {request.request_id[:8]} missing fields: {missing}")
        return request


class TracingStage(PipelineStage):
    name = "tracing"

    async def process(self, request: EngineRequest) -> EngineRequest:
        if not request.trace_id:
            request.trace_id = f"tr-{uuid.uuid4().hex[:12]}"
        request.metadata["trace_id"] = request.trace_id
        return request


class GPUPreprocessStage(PipelineStage):
    name = "gpu_preprocess"

    def __init__(self, enabled: bool = False):
        self._enabled = enabled
        self._processor = get_batch_processor() if enabled else None

    async def process(self, request: EngineRequest) -> EngineRequest:
        if not self._enabled or self._processor is None:
            return request
        # Token pre-processing hook; payload may carry raw token ids for reuse
        if "token_ids" in request.payload:
            processed = self._processor.process_token_batch(request.payload["token_ids"])
            request.payload["token_ids"] = processed
            request.metadata["gpu_preprocessed"] = True
        return request


# ---------------------------------------------------------------------------
# Core engine
# ---------------------------------------------------------------------------


class RuntimeEngine:
    """
    CoreAI Runtime Engine.

    Lifecycle:
        engine = RuntimeEngine()
        await engine.start()
        response = await engine.process(request)
        await engine.stop()

    Or use the async context manager::

        async with RuntimeEngine.create() as engine:
            response = await engine.process(request)
    """

    def __init__(
        self,
        coordinator: Optional[DistributedCoordinator] = None,
        gpu_config: Optional[GPUAccelerationConfig] = None,
    ):
        self._state = EngineState.COLD
        self._engine_id = ENGINE_ID
        self._coordinator = coordinator
        self._gpu_config = gpu_config or GPUAccelerationConfig()
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._pipeline: list[PipelineStage] = []
        self._startup_time: Optional[float] = None
        self._request_count = 0
        self._error_count = 0
        self._total_latency_ms = 0.0

    # ------------------------------------------------------------------
    # Factory / context manager
    # ------------------------------------------------------------------

    @classmethod
    @asynccontextmanager
    async def create(
        cls,
        coordinator: Optional[DistributedCoordinator] = None,
        gpu_config: Optional[GPUAccelerationConfig] = None,
    ) -> AsyncIterator["RuntimeEngine"]:
        engine = cls(coordinator=coordinator, gpu_config=gpu_config)
        await engine.start()
        try:
            yield engine
        finally:
            await engine.stop()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._state not in (EngineState.COLD, EngineState.STOPPED):
            logger.warning("Engine already in state %s — ignoring start()", self._state)
            return

        self._state = EngineState.INITIALIZING
        logger.info("RuntimeEngine %s starting (v%s)...", self._engine_id, ENGINE_VERSION)

        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

        # Build pipeline
        self._pipeline = [
            ValidationStage(),
            TracingStage(),
            GPUPreprocessStage(enabled=ENABLE_GPU),
        ]

        # Start distributed coordinator if enabled
        if ENABLE_DISTRIBUTED:
            if self._coordinator is None:
                self._coordinator = get_coordinator()
            await self._coordinator.start()
            logger.info("Distributed mode enabled — coordinator started")

        self._startup_time = time.time()
        self._state = EngineState.READY
        logger.info("RuntimeEngine %s READY", self._engine_id)

    async def stop(self) -> None:
        if self._state in (EngineState.STOPPED, EngineState.SHUTTING_DOWN):
            return

        self._state = EngineState.SHUTTING_DOWN
        logger.info("RuntimeEngine %s shutting down...", self._engine_id)

        # Drain pipeline stages
        for stage in self._pipeline:
            try:
                await stage.teardown()
            except Exception as exc:  # pylint: disable=broad-except
                logger.error("Stage %s teardown error: %s", stage.name, exc)

        if ENABLE_DISTRIBUTED and self._coordinator:
            await self._coordinator.stop()

        self._state = EngineState.STOPPED
        logger.info(
            "RuntimeEngine %s stopped — handled %d requests (%d errors)",
            self._engine_id,
            self._request_count,
            self._error_count,
        )

    # ------------------------------------------------------------------
    # Request processing
    # ------------------------------------------------------------------

    async def process(self, request: EngineRequest) -> EngineResponse:
        if self._state == EngineState.SHUTTING_DOWN:
            raise EngineShutdownError("Engine is shutting down")
        if self._state != EngineState.READY:
            raise EngineNotReadyError(f"Engine not ready (state={self._state})")

        assert self._semaphore is not None

        async with self._semaphore:
            return await self._process_inner(request)

    async def _process_inner(self, request: EngineRequest) -> EngineResponse:
        t0 = time.perf_counter()
        self._request_count += 1

        try:
            result = await asyncio.wait_for(
                self._run_pipeline(request),
                timeout=request.timeout_s,
            )
            latency_ms = (time.perf_counter() - t0) * 1000
            self._total_latency_ms += latency_ms

            return EngineResponse(
                request_id=request.request_id,
                result=result,
                latency_ms=round(latency_ms, 2),
                gpu_accelerated=request.metadata.get("gpu_preprocessed", False),
                distributed=ENABLE_DISTRIBUTED,
                metadata=request.metadata,
            )

        except asyncio.TimeoutError as exc:
            self._error_count += 1
            raise RequestTimeoutError(
                f"Request {request.request_id[:8]} timed out after {request.timeout_s}s"
            ) from exc

        except Exception as exc:  # pylint: disable=broad-except
            self._error_count += 1
            latency_ms = (time.perf_counter() - t0) * 1000
            logger.error("Request %s failed: %s", request.request_id[:8], exc)
            return EngineResponse(
                request_id=request.request_id,
                result=None,
                latency_ms=round(latency_ms, 2),
                error=str(exc),
            )

    async def _run_pipeline(self, request: EngineRequest) -> Any:
        for stage in self._pipeline:
            request = await stage.process(request)

        # Hand off to distributed coordinator or process locally
        if ENABLE_DISTRIBUTED and self._coordinator:
            task = await self._coordinator.dispatch(request.payload)
            return task.result

        # Local processing — actual provider call happens in coreai.router
        # This engine layer just pre-processes and returns the enriched payload
        return request.payload

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def get_stats(self) -> dict:
        uptime = round(time.time() - self._startup_time, 1) if self._startup_time else 0
        avg_latency = (
            round(self._total_latency_ms / self._request_count, 2)
            if self._request_count > 0
            else 0.0
        )
        return {
            "engine_id": self._engine_id,
            "version": ENGINE_VERSION,
            "state": self._state,
            "uptime_s": uptime,
            "requests": {
                "total": self._request_count,
                "errors": self._error_count,
                "error_rate_pct": round(
                    self._error_count / max(self._request_count, 1) * 100, 2
                ),
            },
            "performance": {
                "avg_latency_ms": avg_latency,
                "max_concurrent": MAX_CONCURRENT_REQUESTS,
            },
            "features": {
                "gpu_enabled": ENABLE_GPU,
                "distributed": ENABLE_DISTRIBUTED,
            },
        }

    @property
    def state(self) -> EngineState:
        return self._state

    @property
    def is_ready(self) -> bool:
        return self._state == EngineState.READY


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_engine: Optional[RuntimeEngine] = None


def get_engine() -> RuntimeEngine:
    global _engine
    if _engine is None:
        _engine = RuntimeEngine()
    return _engine
