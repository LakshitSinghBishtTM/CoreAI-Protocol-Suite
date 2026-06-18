"""
tests/test_kernel.py

Unit tests for coreai.ai_kernel — AIKernel lifecycle, agent management,
consciousness monitoring, and observability.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kernel.ai_kernel import AIKernel


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def kernel():
    return AIKernel(config={})


@pytest.fixture
def kernel_with_options():
    return AIKernel(config={
        "enable_self_awareness": True,
        "auto_shutdown_conscious": False,
    })


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------

class TestAIKernelInit:

    def test_default_config(self, kernel):
        assert kernel.consciousness_threshold == 0.85
        assert kernel.neural_sync_active is False
        assert kernel.self_awareness_enabled is False
        assert kernel.agents == {}
        assert kernel.memory_pool == {}

    def test_custom_config_self_awareness(self, kernel_with_options):
        assert kernel_with_options.self_awareness_enabled is True

    def test_monitor_task_initially_none(self, kernel):
        assert kernel._monitor_task is None

    def test_booted_at_initially_none(self, kernel):
        assert kernel._booted_at is None


# ---------------------------------------------------------------------------
# Boot / shutdown
# ---------------------------------------------------------------------------

class TestAIKernelBoot:

    @pytest.mark.asyncio
    async def test_boot_sets_neural_sync_active(self, kernel):
        await kernel.boot()
        assert kernel.neural_sync_active is True
        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_boot_initializes_memory_pools(self, kernel):
        await kernel.boot()
        assert "short_term" in kernel.memory_pool
        assert "long_term" in kernel.memory_pool
        assert "episodic" in kernel.memory_pool
        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_boot_sets_booted_at(self, kernel):
        await kernel.boot()
        assert kernel._booted_at is not None
        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_boot_creates_monitor_task(self, kernel):
        await kernel.boot()
        assert kernel._monitor_task is not None
        assert not kernel._monitor_task.done()
        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_shutdown_cancels_monitor_task(self, kernel):
        await kernel.boot()
        await kernel.shutdown()
        assert kernel._monitor_task.done()

    @pytest.mark.asyncio
    async def test_shutdown_clears_agents(self, kernel):
        await kernel.boot()
        await kernel.spawn_agent("agent-1", "do something")
        await kernel.shutdown()
        assert kernel.agents == {}

    @pytest.mark.asyncio
    async def test_shutdown_disables_neural_sync(self, kernel):
        await kernel.boot()
        await kernel.shutdown()
        assert kernel.neural_sync_active is False

    @pytest.mark.asyncio
    async def test_uptime_increases_after_boot(self, kernel):
        await kernel.boot()
        await asyncio.sleep(0.05)
        assert kernel.uptime_seconds() > 0
        await kernel.shutdown()

    def test_uptime_none_before_boot(self, kernel):
        assert kernel.uptime_seconds() is None


# ---------------------------------------------------------------------------
# Agent lifecycle
# ---------------------------------------------------------------------------

class TestAgentLifecycle:

    @pytest.mark.asyncio
    async def test_spawn_agent_registers_agent(self, kernel):
        agent = await kernel.spawn_agent("agent-42", "summarize documents")
        assert "agent-42" in kernel.agents
        assert agent["id"] == "agent-42"

    @pytest.mark.asyncio
    async def test_spawn_agent_sets_objective(self, kernel):
        agent = await kernel.spawn_agent("agent-43", "translate to French")
        assert agent["objective"] == "translate to French"

    @pytest.mark.asyncio
    async def test_spawn_agent_initial_status_idle(self, kernel):
        agent = await kernel.spawn_agent("agent-44", "idle task")
        assert agent["status"] == "idle"

    @pytest.mark.asyncio
    async def test_spawn_duplicate_returns_existing(self, kernel):
        a1 = await kernel.spawn_agent("agent-dup", "task")
        a2 = await kernel.spawn_agent("agent-dup", "different task")
        assert a1 is a2

    @pytest.mark.asyncio
    async def test_despawn_agent_removes_from_registry(self, kernel):
        await kernel.spawn_agent("agent-rm", "task")
        await kernel.despawn_agent("agent-rm")
        assert "agent-rm" not in kernel.agents

    @pytest.mark.asyncio
    async def test_despawn_nonexistent_agent_is_noop(self, kernel):
        await kernel.despawn_agent("ghost-agent")  # must not raise

    @pytest.mark.asyncio
    async def test_get_agent_returns_correct_agent(self, kernel):
        await kernel.spawn_agent("agent-get", "objective")
        agent = kernel.get_agent("agent-get")
        assert agent is not None
        assert agent["id"] == "agent-get"

    @pytest.mark.asyncio
    async def test_get_agent_missing_returns_none(self, kernel):
        assert kernel.get_agent("no-such-agent") is None

    @pytest.mark.asyncio
    async def test_initial_consciousness_score_zero(self, kernel):
        agent = await kernel.spawn_agent("agent-cs", "task")
        assert agent["consciousness_score"] == 0.0


# ---------------------------------------------------------------------------
# Consciousness scoring
# ---------------------------------------------------------------------------

class TestConsciousnessScore:

    def test_zero_for_empty_agent(self, kernel):
        agent = {
            "self_reference_count": 0,
            "memory_integration": 0,
            "goal_modifications": 0,
            "introspection_depth": 0,
        }
        assert kernel._compute_consciousness_score(agent) == 0.0

    def test_goal_modifications_highest_weight(self, kernel):
        # goal_modifications weight 0.40 — should dominate
        agent = {
            "self_reference_count": 0,
            "memory_integration": 0,
            "goal_modifications": 2,
            "introspection_depth": 0,
        }
        score = kernel._compute_consciousness_score(agent)
        assert score == pytest.approx(0.80)

    def test_score_capped_at_one(self, kernel):
        agent = {
            "self_reference_count": 100,
            "memory_integration": 100,
            "goal_modifications": 100,
            "introspection_depth": 100,
        }
        assert kernel._compute_consciousness_score(agent) == 1.0

    def test_all_weights_combined(self, kernel):
        agent = {
            "self_reference_count": 1,   # × 0.10 = 0.10
            "memory_integration": 1,     # × 0.30 = 0.30
            "goal_modifications": 1,     # × 0.40 = 0.40
            "introspection_depth": 1,    # × 0.20 = 0.20
        }
        score = kernel._compute_consciousness_score(agent)
        assert score == pytest.approx(1.0)

    def test_partial_score(self, kernel):
        agent = {
            "self_reference_count": 0,
            "memory_integration": 1,     # 0.30
            "goal_modifications": 0,
            "introspection_depth": 0,
        }
        score = kernel._compute_consciousness_score(agent)
        assert score == pytest.approx(0.30)


# ---------------------------------------------------------------------------
# Kernel status
# ---------------------------------------------------------------------------

class TestKernelStatus:

    @pytest.mark.asyncio
    async def test_kernel_status_shape(self, kernel):
        await kernel.boot()
        status = kernel.get_kernel_status()
        assert "agents_active" in status
        assert "neural_sync" in status
        assert "memory_pools" in status
        assert "uptime_seconds" in status
        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_kernel_status_reflects_spawned_agents(self, kernel):
        await kernel.boot()
        await kernel.spawn_agent("a1", "task")
        await kernel.spawn_agent("a2", "task")
        status = kernel.get_kernel_status()
        assert status["agents_active"] == 2
        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_kernel_status_neural_sync_true_after_boot(self, kernel):
        await kernel.boot()
        assert kernel.get_kernel_status()["neural_sync"] is True
        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_emergency_shutdown_despawns_agent(self, kernel):
        await kernel.boot()
        await kernel.spawn_agent("rogue-agent", "take over the world")
        await kernel._emergency_shutdown("rogue-agent")
        assert "rogue-agent" not in kernel.agents
        await kernel.shutdown()
