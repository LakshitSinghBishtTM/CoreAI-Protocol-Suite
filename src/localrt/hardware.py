"""Hardware detection and llama-server launch planning."""

import os
import platform
import shutil
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class Hardware:
    backend: str  # cuda | metal | cpu
    ram_bytes: int
    vram_bytes: int = 0  # 0 for cpu; for metal the shared budget


def _ram() -> int:
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError, AttributeError):
        return 8 * 1024**3


def _nvidia_vram() -> int:
    if not shutil.which("nvidia-smi"):
        return 0
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout  # fmt: skip
        return int(out.splitlines()[0].strip()) * 1024**2
    except (subprocess.SubprocessError, ValueError, IndexError, OSError):
        return 0


def detect() -> Hardware:
    ram = _ram()
    vram = _nvidia_vram()
    if vram:
        return Hardware("cuda", ram, vram)
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        return Hardware("metal", ram, int(ram * 0.65))
    return Hardware("cpu", ram, 0)


def kv_cache_bytes(ctx: int, n_layers: int = 32, embd: int = 4096) -> int:
    """Rough fp16 KV-cache size: 2 (K,V) * layers * ctx * embd * 2 bytes."""
    return 2 * n_layers * ctx * embd * 2


def plan(
    model_bytes: int,
    trained_ctx: int,
    n_layers: int,
    hw: Hardware,
    *,
    ctx_override: int = 0,
    ngl_override: int | None = None,
    parallel: int = 1,
    headroom: float = 0.85,
) -> dict:
    """Pick context size and GPU layers so the model fits memory.

    Returns ``{"ctx", "n_gpu_layers", "fits"}``. Context shrinks (down to 2048)
    before layers are moved off the GPU; ``fits`` is False if even that overflows.
    """
    budget = int((hw.vram_bytes or hw.ram_bytes) * headroom)
    ctx = ctx_override or min(trained_ctx or 4096, 8192)
    per_layer = model_bytes / max(n_layers, 1)

    def need(c: int, layers: int) -> int:
        return int(per_layer * layers + kv_cache_bytes(c * parallel))

    while not ctx_override and ctx > 2048 and need(ctx, n_layers) > budget:
        ctx //= 2
    if hw.backend == "cpu":
        layers = 0
        fits = model_bytes + kv_cache_bytes(ctx * parallel) <= budget
    else:
        layers = n_layers
        while layers > 0 and need(ctx, layers) > budget:
            layers -= 1
        fits = layers > 0 or model_bytes <= hw.ram_bytes * headroom
    if ngl_override is not None:
        layers = ngl_override
    return {"ctx": ctx, "n_gpu_layers": layers, "fits": fits}
