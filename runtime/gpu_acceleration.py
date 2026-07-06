"""
runtime/gpu_acceleration.py

GPU acceleration layer for CoreAI inference pipeline.
Handles CUDA device management, memory pooling, and batch
throughput optimization for LLM provider request pre/post-processing.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CUDA_DEVICE_ENV = os.environ.get("COREAI_CUDA_DEVICE", "cuda:0")
GPU_MEMORY_FRACTION = float(os.environ.get("COREAI_GPU_MEMORY_FRACTION", "0.85"))
BATCH_SIZE_DEFAULT = int(os.environ.get("COREAI_GPU_BATCH_SIZE", "32"))
WARMUP_ITERS = 3


@dataclass
class GPUDeviceInfo:
    device_id: int
    name: str
    total_memory_mb: int
    allocated_memory_mb: int = 0
    utilization_pct: float = 0.0
    driver_version: str = "unknown"
    cuda_version: str = "unknown"


@dataclass
class GPUAccelerationConfig:
    device: str = CUDA_DEVICE_ENV
    memory_fraction: float = GPU_MEMORY_FRACTION
    batch_size: int = BATCH_SIZE_DEFAULT
    enable_mixed_precision: bool = True
    enable_tensor_cores: bool = True
    warmup_on_init: bool = True
    pin_memory: bool = True
    num_workers: int = 4


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class GPUNotAvailableError(RuntimeError):
    """Raised when no suitable CUDA device is found."""


class GPUMemoryError(RuntimeError):
    """Raised when GPU OOM is encountered during batch processing."""


# ---------------------------------------------------------------------------
# Device manager
# ---------------------------------------------------------------------------


class GPUDeviceManager:
    """
    Manages CUDA device lifecycle, memory reservation, and health checks.
    Falls back to CPU silently when CUDA is unavailable.
    """

    _instance: Optional["GPUDeviceManager"] = None

    def __init__(self, config: GPUAccelerationConfig):
        self.config = config
        self._device_info: Optional[GPUDeviceInfo] = None
        self._initialized = False
        self._fallback_cpu = False

    @classmethod
    def get_instance(
        cls, config: Optional[GPUAccelerationConfig] = None
    ) -> "GPUDeviceManager":
        if cls._instance is None:
            cls._instance = cls(config or GPUAccelerationConfig())
        return cls._instance

    def initialize(self) -> bool:
        try:
            import torch  # type: ignore

            if not torch.cuda.is_available():
                logger.warning("CUDA not available — falling back to CPU mode")
                self._fallback_cpu = True
                return False

            device_idx = (
                int(self.config.device.split(":")[-1])
                if ":" in self.config.device
                else 0
            )
            props = torch.cuda.get_device_properties(device_idx)

            self._device_info = GPUDeviceInfo(
                device_id=device_idx,
                name=props.name,
                total_memory_mb=props.total_memory // (1024**2),
                driver_version=torch.version.cuda or "unknown",
                cuda_version=torch.version.cuda or "unknown",
            )

            torch.cuda.set_per_process_memory_fraction(
                self.config.memory_fraction, device=device_idx
            )

            if self.config.enable_mixed_precision:
                torch.backends.cuda.matmul.allow_tf32 = True
                torch.backends.cudnn.allow_tf32 = True

            if self.config.warmup_on_init:
                self._warmup(torch, device_idx)

            self._initialized = True
            logger.info(
                "GPU initialised: %s | %.1f GB reserved",
                props.name,
                (props.total_memory * self.config.memory_fraction) / (1024**3),
            )
            return True

        except ImportError:
            logger.warning("PyTorch not installed — GPU acceleration disabled")
            self._fallback_cpu = True
            return False
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("GPU init failed: %s", exc)
            self._fallback_cpu = True
            return False

    def _warmup(self, torch, device_idx: int) -> None:
        logger.debug("Running GPU warmup (%d iters)...", WARMUP_ITERS)
        device = torch.device(f"cuda:{device_idx}")
        for _ in range(WARMUP_ITERS):
            dummy = torch.randn(512, 512, device=device)
            _ = dummy @ dummy.T
        torch.cuda.synchronize()
        logger.debug("GPU warmup complete")

    def get_device_info(self) -> Optional[GPUDeviceInfo]:
        if not self._initialized:
            return None
        try:
            import torch  # type: ignore

            idx = self._device_info.device_id  # type: ignore
            self._device_info.allocated_memory_mb = (  # type: ignore
                torch.cuda.memory_allocated(idx) // (1024**2)
            )
            self._device_info.utilization_pct = (  # type: ignore
                self._device_info.allocated_memory_mb  # type: ignore
                / self._device_info.total_memory_mb  # type: ignore
                * 100
            )
        except Exception:  # pylint: disable=broad-except
            pass
        return self._device_info

    def free_cache(self) -> None:
        try:
            import torch  # type: ignore

            torch.cuda.empty_cache()
            logger.debug("GPU cache cleared")
        except Exception:  # pylint: disable=broad-except
            pass

    @property
    def is_available(self) -> bool:
        return self._initialized and not self._fallback_cpu

    @property
    def is_cpu_fallback(self) -> bool:
        return self._fallback_cpu


# ---------------------------------------------------------------------------
# Batch processor
# ---------------------------------------------------------------------------


@dataclass
class BatchProcessingStats:
    total_batches: int = 0
    total_tokens_processed: int = 0
    avg_batch_latency_ms: float = 0.0
    peak_memory_mb: int = 0
    oom_events: int = 0
    _latency_sum: float = field(default=0.0, repr=False)

    def record(self, tokens: int, latency_ms: float, memory_mb: int) -> None:
        self.total_batches += 1
        self.total_tokens_processed += tokens
        self._latency_sum += latency_ms
        self.avg_batch_latency_ms = self._latency_sum / self.total_batches
        if memory_mb > self.peak_memory_mb:
            self.peak_memory_mb = memory_mb


class GPUBatchProcessor:
    """
    Batches token-level pre/post-processing work onto the GPU to reduce
    per-request overhead before handing off to provider HTTP clients.
    """

    def __init__(
        self,
        config: Optional[GPUAccelerationConfig] = None,
        device_manager: Optional[GPUDeviceManager] = None,
    ):
        self.config = config or GPUAccelerationConfig()
        self.device_manager = device_manager or GPUDeviceManager.get_instance(
            self.config
        )
        self.stats = BatchProcessingStats()
        self._queue: list = []

    def process_token_batch(self, token_ids: list[list[int]]) -> list[list[int]]:
        """
        Processes a batch of token ID sequences.
        Falls back to identity pass-through when GPU is unavailable.
        """
        if not self.device_manager.is_available:
            return token_ids

        t0 = time.perf_counter()
        result = self._gpu_process(token_ids)
        latency_ms = (time.perf_counter() - t0) * 1000

        info = self.device_manager.get_device_info()
        mem_mb = info.allocated_memory_mb if info else 0
        token_count = sum(len(seq) for seq in token_ids)
        self.stats.record(token_count, latency_ms, mem_mb)

        return result

    def _gpu_process(self, token_ids: list[list[int]]) -> list[list[int]]:
        try:
            import torch  # type: ignore

            device = torch.device(self.config.device)
            max_len = max(len(seq) for seq in token_ids)
            padded = [seq + [0] * (max_len - len(seq)) for seq in token_ids]
            tensor = torch.tensor(padded, dtype=torch.long, device=device)
            # Placeholder: real work (embedding lookup, positional encoding prep) goes here
            result_tensor = tensor.cpu()
            return result_tensor.tolist()
        except RuntimeError as exc:
            if "out of memory" in str(exc).lower():
                self.stats.oom_events += 1
                self.device_manager.free_cache()
                logger.warning(
                    "GPU OOM during batch — clearing cache and retrying on CPU"
                )
                return token_ids
            raise

    def get_stats(self) -> dict:
        return {
            "total_batches": self.stats.total_batches,
            "total_tokens_processed": self.stats.total_tokens_processed,
            "avg_batch_latency_ms": round(self.stats.avg_batch_latency_ms, 2),
            "peak_memory_mb": self.stats.peak_memory_mb,
            "oom_events": self.stats.oom_events,
            "gpu_available": self.device_manager.is_available,
            "cpu_fallback": self.device_manager.is_cpu_fallback,
        }


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def get_device_manager(
    config: Optional[GPUAccelerationConfig] = None,
) -> GPUDeviceManager:
    mgr = GPUDeviceManager.get_instance(config)
    if not mgr._initialized:
        mgr.initialize()
    return mgr


def get_batch_processor(
    config: Optional[GPUAccelerationConfig] = None,
) -> GPUBatchProcessor:
    mgr = get_device_manager(config)
    return GPUBatchProcessor(config=config, device_manager=mgr)
