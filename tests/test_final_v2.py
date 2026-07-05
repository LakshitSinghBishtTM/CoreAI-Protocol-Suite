"""
tests/test_final_v2.py

End-to-end integration tests for the CoreAI high-level facade (CoreAI class
in core_final.py) and the Kernel. All provider/router calls are mocked.
"""

from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest

from providers.base import CompletionResponse


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_response(content="ok", provider="openai", model="gpt-4o-mini"):
    return CompletionResponse(
        content=content,
        model=model,
        provider=provider,
        input_tokens=20,
        output_tokens=10,
        cost_usd=0.0000030,
        latency_ms=198.7,
    )


def _mock_kernel(state="running"):
    k = MagicMock()
    k.state = state
    k.start = AsyncMock()
    k.stop = AsyncMock()
    k.router = MagicMock()
    k.router.route = AsyncMock(return_value=_mock_response())
    k.orchestrator = MagicMock()
    k.stats = MagicMock(return_value={"kernel": {}, "router": {}, "orchestrator": {}})
    k.health = MagicMock(return_value={"state": state})
    return k


# ---------------------------------------------------------------------------
# CoreAI facade
# ---------------------------------------------------------------------------

class TestCoreAIFacade:

    @pytest.fixture
    def ai_class(self):
        """Patch boot() so no real providers are loaded.
        Import via package path to preserve relative imports inside coreai/.
        """
        with patch("coreai.core_final.boot") as mock_boot, \
             patch("coreai.core_final.Kernel") as MockKernel:
            mock_kernel = _mock_kernel()
            MockKernel.return_value = mock_kernel
            mock_boot.return_value = (MagicMock(), MagicMock())
            from coreai.core_final import CoreAI
            yield CoreAI, mock_kernel

    @pytest.mark.asyncio
    async def test_start_calls_kernel_start(self, ai_class):
        CoreAI, mock_kernel = ai_class
        ai = CoreAI()
        await ai.start()
        mock_kernel.start.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_calls_kernel_stop(self, ai_class):
        CoreAI, mock_kernel = ai_class
        ai = CoreAI()
        await ai.start()
        await ai.stop()
        mock_kernel.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_complete_routes_to_kernel(self, ai_class):
        CoreAI, mock_kernel = ai_class
        async with CoreAI() as ai:
            result = await ai.complete("What is entropy?")
        assert result.content == "ok"
        mock_kernel.router.route.assert_called_once()

    @pytest.mark.asyncio
    async def test_complete_with_system_prompt(self, ai_class):
        CoreAI, mock_kernel = ai_class
        async with CoreAI() as ai:
            await ai.complete("Explain gradient descent.", system="Be terse.")
        call_args = mock_kernel.router.route.call_args[0][0]
        roles = [m.role for m in call_args.messages]
        assert "system" in roles

    @pytest.mark.asyncio
    async def test_chat_passes_messages(self, ai_class):
        CoreAI, mock_kernel = ai_class
        async with CoreAI() as ai:
            await ai.chat([
                {"role": "system", "content": "You are helpful."},
                {"role": "user", "content": "Hi"},
            ])
        call_args = mock_kernel.router.route.call_args[0][0]
        assert len(call_args.messages) == 2

    @pytest.mark.asyncio
    async def test_context_manager_starts_and_stops(self, ai_class):
        CoreAI, mock_kernel = ai_class
        async with CoreAI():
            pass
        mock_kernel.start.assert_called_once()
        mock_kernel.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_stats_returns_dict(self, ai_class):
        CoreAI, mock_kernel = ai_class
        async with CoreAI() as ai:
            s = ai.stats()
        assert isinstance(s, dict)

    @pytest.mark.asyncio
    async def test_health_returns_dict(self, ai_class):
        CoreAI, mock_kernel = ai_class
        async with CoreAI() as ai:
            h = ai.health()
        assert "state" in h

    def test_invalid_strategy_raises(self):
        with patch("coreai.core_final.boot"), patch("coreai.core_final.Kernel"):
            from coreai.core_final import CoreAI
            with pytest.raises(ValueError, match="Invalid strategy"):
                CoreAI(strategy="teleport")

    @pytest.mark.asyncio
    async def test_complete_before_start_raises(self):
        with patch("coreai.core_final.boot"), patch("coreai.core_final.Kernel"):
            from coreai.core_final import CoreAI
            ai = CoreAI()
            with pytest.raises(RuntimeError, match="not started"):
                await ai.complete("hello")

    @pytest.mark.asyncio
    async def test_register_agent_delegates_to_orchestrator(self, ai_class):
        CoreAI, mock_kernel = ai_class
        async with CoreAI() as ai:
            ai.register_agent("agent-99", {"model": "gpt-4o"})
        mock_kernel.orchestrator.register_agent.assert_called_once_with(
            "agent-99", {"model": "gpt-4o"}
        )

    @pytest.mark.asyncio
    async def test_assign_task_delegates_to_orchestrator(self, ai_class):
        CoreAI, mock_kernel = ai_class
        mock_kernel.orchestrator.assign_task.return_value = MagicMock(task_id="t-001")
        async with CoreAI() as ai:
            task = ai.assign_task("agent-1", "Summarize the logs", {"limit": 100})
        mock_kernel.orchestrator.assign_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_task_delegates_to_orchestrator(self, ai_class):
        CoreAI, mock_kernel = ai_class
        mock_kernel.orchestrator.get_task.return_value = MagicMock(task_id="t-007")
        async with CoreAI() as ai:
            task = ai.get_task("t-007")
        mock_kernel.orchestrator.get_task.assert_called_once_with("t-007")


# ---------------------------------------------------------------------------
# Kernel (coreai/kernel.py) basic wiring
# Fix: import from coreai.kernel not bare 'kernel' (which resolves to kernel/)
# ---------------------------------------------------------------------------

class TestKernelWiring:

    @pytest.fixture
    def components(self):
        router = MagicMock()
        router.providers = {"openai": MagicMock(), "anthropic": MagicMock()}
        router.stats = MagicMock(return_value={
            "total_requests": 0,
            "strategy": "balanced",
            "provider_stats": {},
        })

        orchestrator = MagicMock()
        orchestrator.get_pending_tasks = MagicMock(return_value=[])
        orchestrator.get_active_tasks = MagicMock(return_value=[])
        orchestrator.stats = MagicMock(return_value={})

        memory = MagicMock()
        memory.start = AsyncMock()
        memory.stop = AsyncMock()
        memory.stats = MagicMock(return_value={})

        scheduler = MagicMock()
        scheduler.start = AsyncMock()
        scheduler.stop = AsyncMock()
        scheduler.stats = MagicMock(return_value={})

        return router, orchestrator, memory, scheduler

    @pytest.mark.asyncio
    async def test_start_transitions_to_running(self, components):
        from coreai.kernel import Kernel, KernelState
        router, orchestrator, memory, scheduler = components
        k = Kernel(router, orchestrator, memory, scheduler)
        await k.start()
        assert k.state == KernelState.RUNNING
        await k.stop()

    @pytest.mark.asyncio
    async def test_stop_transitions_to_stopped(self, components):
        from coreai.kernel import Kernel, KernelState
        router, orchestrator, memory, scheduler = components
        k = Kernel(router, orchestrator, memory, scheduler)
        await k.start()
        await k.stop()
        assert k.state == KernelState.STOPPED

    @pytest.mark.asyncio
    async def test_double_stop_is_idempotent(self, components):
        from coreai.kernel import Kernel
        router, orchestrator, memory, scheduler = components
        k = Kernel(router, orchestrator, memory, scheduler)
        await k.start()
        await k.stop()
        await k.stop()  # should not raise

    @pytest.mark.asyncio
    async def test_uptime_increases(self, components):
        import asyncio
        from coreai.kernel import Kernel
        router, orchestrator, memory, scheduler = components
        k = Kernel(router, orchestrator, memory, scheduler)
        await k.start()
        await asyncio.sleep(0.05)
        assert k.uptime_seconds() > 0
        await k.stop()

    @pytest.mark.asyncio
    async def test_health_contains_expected_keys(self, components):
        from coreai.kernel import Kernel
        router, orchestrator, memory, scheduler = components
        k = Kernel(router, orchestrator, memory, scheduler)
        await k.start()
        h = k.health()
        for key in ("state", "uptime_seconds", "providers", "active_tasks"):
            assert key in h, f"Missing key: {key}"
        await k.stop()

    @pytest.mark.asyncio
    async def test_stats_includes_router_and_orchestrator(self, components):
        from coreai.kernel import Kernel
        router, orchestrator, memory, scheduler = components
        k = Kernel(router, orchestrator, memory, scheduler)
        await k.start()
        s = k.stats()
        assert "router" in s
        assert "orchestrator" in s
        await k.stop()

    @pytest.mark.asyncio
    async def test_cancels_pending_tasks_on_stop(self, components):
        from coreai.kernel import Kernel
        router, orchestrator, memory, scheduler = components
        fake_task = MagicMock(task_id="t-pending-001")
        orchestrator.get_pending_tasks.return_value = [fake_task]
        k = Kernel(router, orchestrator, memory, scheduler)
        await k.start()
        await k.stop()
        orchestrator.cancel_task.assert_called_once_with("t-pending-001")