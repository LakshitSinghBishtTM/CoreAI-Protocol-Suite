"""
tests/test_agent_manager.py

Smoke tests for agents/agent_manager.py. Agents are experimental (see
agents/README.md); these tests pin the lifecycle behaviour that exists today
so the Phase 7 rebuild can change it deliberately.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents import agent_manager as am


class FakeAgent:
    """Stand-in for AutonomousAgent: no kernel, memory or tools required."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.healthy = True
        self.initialized = False

    async def initialize(self):
        self.initialized = True

    async def health_check(self):
        return self.healthy


@pytest.fixture
def manager():
    protocol = MagicMock()
    protocol.broadcast_heartbeat = AsyncMock()
    with (
        patch.object(am, "AutonomousAgent", FakeAgent),
        patch.object(am, "get_agent_protocol", return_value=protocol),
    ):
        mgr = am.AgentManager(kernel=MagicMock(), memory_manager=MagicMock())
        yield mgr


def test_manager_has_no_consciousness_detector(manager):
    # The consciousness detection module was archived in Phase 1.
    assert not hasattr(manager, "consciousness")
    assert not hasattr(manager, "get_pending_anomalies")


@pytest.mark.asyncio
async def test_spawn_registers_agent_and_marks_idle(manager):
    agent_id = await manager.spawn_agent("worker", ["memory_read"])
    record = manager.agents[agent_id]
    assert record.status == am.AgentStatus.IDLE
    assert record.agent.initialized is True
    manager.protocol.register_agent.assert_called_once()


@pytest.mark.asyncio
async def test_spawn_enforces_agent_limit(manager):
    manager.MAX_AGENTS = 2
    await manager.spawn_agent("a", [])
    await manager.spawn_agent("b", [])
    with pytest.raises(RuntimeError, match="Agent limit reached"):
        await manager.spawn_agent("c", [])


@pytest.mark.asyncio
async def test_failed_initialize_marks_agent_failed(manager):
    async def boom(self):
        raise ValueError("init failed")

    with patch.object(FakeAgent, "initialize", boom):
        with pytest.raises(ValueError):
            await manager.spawn_agent("broken", [])
    (record,) = manager.agents.values()
    assert record.status == am.AgentStatus.FAILED


@pytest.mark.asyncio
async def test_health_check_broadcasts_heartbeat_for_healthy_agent(manager):
    agent_id = await manager.spawn_agent("worker", [])
    manager._restart_agent = AsyncMock()
    await manager._check_all_agents()
    manager._restart_agent.assert_not_called()
    manager.protocol.broadcast_heartbeat.assert_awaited_once()
    assert manager.protocol.broadcast_heartbeat.await_args.args[0] == agent_id


@pytest.mark.asyncio
async def test_health_check_restarts_unhealthy_agent(manager):
    agent_id = await manager.spawn_agent("worker", [])
    manager.agents[agent_id].agent.healthy = False
    manager._restart_agent = AsyncMock()
    await manager._check_all_agents()
    manager._restart_agent.assert_awaited_once_with(agent_id)


@pytest.mark.asyncio
async def test_heartbeat_failure_does_not_stop_the_health_loop(manager):
    await manager.spawn_agent("a", [])
    await manager.spawn_agent("b", [])
    manager.protocol.broadcast_heartbeat.side_effect = RuntimeError("bus down")
    await manager._check_all_agents()  # must not raise
    assert manager.protocol.broadcast_heartbeat.await_count == 2
