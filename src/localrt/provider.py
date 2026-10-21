"""Gateway provider for GGUF models the supervisor runs as child processes."""

import asyncio
import shutil
import time
from typing import Any, AsyncGenerator, Callable, Optional

import httpx
from openai import AsyncOpenAI

from providers.base import CompletionRequest, CompletionResponse
from providers.local import (
    CONNECT_TIMEOUT_S,
    READ_TIMEOUT_S,
    Capabilities,
    LocalProvider,
)

from . import hardware
from .registry import ModelRegistry
from .supervisor import Supervisor

LOAD_TIMEOUT_S = 120.0


async def http_ready(base_url: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=2) as c:
            r = await c.get(base_url.removesuffix("/v1") + "/health")
            return r.status_code == 200
    except httpx.HTTPError:
        return False


def _sync_healthy(base_url: str) -> bool:
    try:
        return httpx.get(
            base_url.removesuffix("/v1") + "/health", timeout=3
        ).status_code in (200, 503)
    except httpx.HTTPError:
        return False  # 503 = still loading, which is not hung


class ManagedLocalProvider(LocalProvider):
    """Models are the .gguf files in a directory; one child process per loaded model."""

    def __init__(
        self,
        name: str,
        registry: ModelRegistry,
        supervisor: Supervisor,
        binary: str = "",
        hw: Optional[hardware.Hardware] = None,
        max_queue: int = 0,
        ready: Callable[[str], Any] = http_ready,
        load_timeout_s: float = LOAD_TIMEOUT_S,
    ):
        registry.scan()
        super().__init__(
            name=name,
            base_url="http://127.0.0.1:0/v1",
            models=list(registry._models),
            max_queue=max_queue,
        )
        self.registry, self.supervisor = registry, supervisor
        self.binary = binary or shutil.which("llama-server") or ""
        self.hw = hw or hardware.detect()
        self._ready, self.load_timeout_s = ready, load_timeout_s
        self._lock = asyncio.Lock()

    def capabilities(self, model: Optional[str]) -> Capabilities:
        info = self.registry.get(model or self.default_model)
        if not info:
            return Capabilities()
        c = info["capabilities"]
        return Capabilities(
            tools=c["tools"],
            vision=c["vision"],
            json_mode=c["json_mode"],
            context_length=info.get("context_length"),
        )

    async def probe(self) -> bool:
        """Runs every few seconds: unload idle models, kill hung children (they
        restart on the next request), and report whether we can launch at all."""
        self.supervisor.reap_idle()
        await asyncio.to_thread(self.supervisor.unhealthy, _sync_healthy)
        self.up = bool(self.binary)  # models load on demand, so "up" = can launch
        return self.up

    async def refresh_models(self) -> list[str]:
        self.models = set(self.registry.scan())
        return sorted(self.models)

    async def _prepare(self, model: str) -> None:
        info = self.registry.get(model)
        if not info:
            raise ValueError(f"Unknown local model '{model}'.")
        if not self.binary:
            raise RuntimeError(
                "llama-server not found; install it or set binary in config."
            )
        plan = hardware.plan(
            info["size_bytes"],
            info.get("context_length") or 4096,
            info.get("n_layers") or 32,
            self.hw,
        )
        if not plan["fits"]:
            raise RuntimeError(f"Model '{model}' does not fit in available memory.")
        async with self._lock:
            url = await asyncio.to_thread(
                self.supervisor.ensure, model, self.binary, info["path"], plan
            )
            deadline = time.monotonic() + self.load_timeout_s
            while not await self._ready(url):
                if time.monotonic() > deadline:
                    raise TimeoutError(f"Model '{model}' did not become ready in time.")
                await asyncio.sleep(0.5)
        if url != self.base_url:
            self.base_url = url
            self.client = AsyncOpenAI(
                api_key=self.api_key,
                base_url=url,
                timeout=httpx.Timeout(READ_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
                max_retries=0,
            )

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        await self._prepare(self._need_model(request))
        return await super().complete(request)

    async def stream(self, request: CompletionRequest) -> AsyncGenerator[str, None]:
        await self._prepare(self._need_model(request))
        async for piece in super().stream(request):
            yield piece
