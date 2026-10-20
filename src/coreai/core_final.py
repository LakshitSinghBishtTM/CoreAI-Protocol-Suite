"""
CoreAI Protocol Suite - Core
Top-level convenience interface. Import from here for the simplest usage.

    from coreai.core_final import CoreAI

    ai = CoreAI()
    await ai.start()
    response = await ai.complete("Explain transformers in one paragraph.")
    print(response.content)
    await ai.stop()
"""

import asyncio
from typing import Optional

from providers import CompletionRequest, CompletionResponse, Message

from .bootloader import BootConfig, boot
from .kernel import Kernel
from .orchestrator import AgentTask
from .router import RoutingStrategy


class CoreAI:
    """
    High-level facade over the full CoreAI stack.
    Wraps Kernel, Router, Orchestrator, and MemoryManager behind a simple API.

    Usage (async context manager):

        async with CoreAI() as ai:
            response = await ai.complete("Hello, world.")
            print(response.content)

    Usage (manual):

        ai = CoreAI(strategy="cheapest")
        await ai.start()
        response = await ai.complete("Hello.")
        await ai.stop()
    """

    def __init__(
        self,
        strategy: str = "balanced",
        enable_cache: bool = True,
        enable_retry: bool = True,
        required_providers: Optional[list] = None,
    ):
        try:
            routing_strategy = RoutingStrategy(strategy)
        except ValueError:
            valid = [s.value for s in RoutingStrategy]
            raise ValueError(f"Invalid strategy '{strategy}'. Valid: {valid}")

        self._boot_config = BootConfig(
            strategy=routing_strategy,
            enable_cache=enable_cache,
            enable_retry=enable_retry,
            required_providers=required_providers,
        )
        self._kernel: Optional[Kernel] = None

    async def start(self):
        """Boot the system and start the kernel."""
        router, orchestrator = boot(self._boot_config)
        self._kernel = Kernel(router=router, orchestrator=orchestrator)
        await self._kernel.start()

    async def stop(self):
        """Graceful shutdown."""
        if self._kernel:
            await self._kernel.stop()

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *_):
        await self.stop()

    # ------------------------------------------------------------------ #
    # Completions
    # ------------------------------------------------------------------ #

    async def complete(
        self,
        prompt: str,
        *,
        system: Optional[str] = None,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        max_tokens: int = 1024,
        temperature: float = 0.7,
    ) -> CompletionResponse:
        """
        Single-turn completion. Simplest entry point.

            response = await ai.complete("What is entropy?")
        """
        self._require_started()
        messages = []
        if system:
            messages.append(Message(role="system", content=system))
        messages.append(Message(role="user", content=prompt))

        request = CompletionRequest(
            messages=messages,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return await self._kernel.router.route(request, preferred_provider=provider)

    async def chat(
        self,
        messages: list[dict],
        *,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        max_tokens: int = 1024,
        temperature: float = 0.7,
    ) -> CompletionResponse:
        """
        Multi-turn chat completion. Pass messages as list of dicts.

            response = await ai.chat([
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user",   "content": "Explain gradient descent."},
            ])
        """
        self._require_started()
        parsed = [Message(role=m["role"], content=m["content"]) for m in messages]
        request = CompletionRequest(
            messages=parsed,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return await self._kernel.router.route(request, preferred_provider=provider)

    # ------------------------------------------------------------------ #
    # Agents
    # ------------------------------------------------------------------ #

    def register_agent(self, agent_id: str, config: Optional[dict] = None):
        """Register an agent for task assignment."""
        self._require_started()
        self._kernel.orchestrator.register_agent(agent_id, config)

    def assign_task(
        self,
        agent_id: str,
        objective: str,
        context: Optional[dict] = None,
    ) -> AgentTask:
        """Assign a task to a registered agent."""
        self._require_started()
        return self._kernel.orchestrator.assign_task(agent_id, objective, context)

    def get_task(self, task_id: str) -> Optional[AgentTask]:
        self._require_started()
        return self._kernel.orchestrator.get_task(task_id)

    # ------------------------------------------------------------------ #
    # Observability
    # ------------------------------------------------------------------ #

    def stats(self) -> dict:
        """Full system stats — router, cache, orchestrator, memory, scheduler."""
        self._require_started()
        return self._kernel.stats()

    def health(self) -> dict:
        """Kernel health snapshot."""
        self._require_started()
        return self._kernel.health()

    # ------------------------------------------------------------------ #
    # Internal
    # ------------------------------------------------------------------ #

    def _require_started(self):
        if self._kernel is None or self._kernel.state not in ("running", "degraded"):
            raise RuntimeError("CoreAI not started. Call await ai.start() first.")


# ------------------------------------------------------------------ #
# Convenience: run a single completion from the CLI / scripts
# ------------------------------------------------------------------ #


async def _quick_complete(prompt: str):
    async with CoreAI() as ai:
        response = await ai.complete(prompt)
        print(f"\nModel   : {response.model}")
        print(f"Provider: {response.provider}")
        print(f"Cost    : ${response.cost_usd:.6f}")
        print(f"Latency : {response.latency_ms:.0f}ms")
        print(f"Cached  : {response.cached}")
        print(f"\n{response.content}\n")


if __name__ == "__main__":
    import sys

    prompt = " ".join(sys.argv[1:]) or "Say hello."
    asyncio.run(_quick_complete(prompt))
