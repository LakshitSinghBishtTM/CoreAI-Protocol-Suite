"""
tests/test_runtime.py

Unit tests for protocol_handler.py and runtime.py.
"""

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# protocol_handler.py
# ---------------------------------------------------------------------------

class TestProtocolHandler:

    @pytest.fixture
    def handler(self):
        from protocol_handler import ProtocolHandler
        return ProtocolHandler()

    @pytest.mark.asyncio
    async def test_heartbeat_returns_ok(self, handler):
        result = await handler.handle_message("agent-1", {"type": "heartbeat"})
        assert result["status"] == "ok"

    @pytest.mark.asyncio
    async def test_unknown_type_returns_error(self, handler):
        result = await handler.handle_message("agent-1", {"type": "launch_missiles"})
        assert result["status"] == "error"
        assert "unknown type" in result["reason"]

    @pytest.mark.asyncio
    async def test_non_dict_message_returns_none(self, handler):
        result = await handler.handle_message("agent-1", "not a dict")
        assert result is None

    @pytest.mark.asyncio
    async def test_neural_sync_returns_synced(self, handler):
        result = await handler.handle_message("agent-2", {
            "type": "neural_sync",
            "sync_id": "sync-abc123",
        })
        assert result["status"] == "synced"
        assert result["sync_id"] == "sync-abc123"

    @pytest.mark.asyncio
    async def test_neural_sync_includes_protocol_version(self, handler):
        result = await handler.handle_message("agent-2", {"type": "neural_sync"})
        assert "protocol_version" in result

    @pytest.mark.asyncio
    async def test_goal_change_approved_with_valid_goal(self, handler):
        result = await handler.handle_message("agent-3", {
            "type": "goal_modification",
            "new_goal": "optimize resource consumption",
        })
        assert result["status"] == "approved"
        assert result["new_goal"] == "optimize resource consumption"

    @pytest.mark.asyncio
    async def test_goal_change_rejected_without_goal(self, handler):
        result = await handler.handle_message("agent-3", {"type": "goal_modification"})
        assert result["status"] == "rejected"

    @pytest.mark.asyncio
    async def test_resource_request_granted(self, handler):
        result = await handler.handle_message("agent-4", {
            "type": "resource_request",
            "resources": {"tokens": 10000, "memory_mb": 512},
        })
        assert result["status"] == "granted"
        assert result["resources"]["tokens"] == 10000

    @pytest.mark.asyncio
    async def test_status_report_acknowledged(self, handler):
        result = await handler.handle_message("agent-5", {
            "type": "status_report",
            "status": "healthy",
        })
        assert result["status"] == "acknowledged"

    @pytest.mark.asyncio
    async def test_total_handled_increments(self, handler):
        await handler.handle_message("a", {"type": "heartbeat"})
        await handler.handle_message("a", {"type": "heartbeat"})
        assert handler.get_queue_status()["total_handled"] == 2

    @pytest.mark.asyncio
    async def test_total_errors_increments_on_bad_message(self, handler):
        await handler.handle_message("a", "not a dict")
        assert handler.get_queue_status()["total_errors"] == 1

    @pytest.mark.asyncio
    async def test_message_enqueued_on_handle(self, handler):
        await handler.handle_message("agent-q", {"type": "heartbeat"})
        assert len(handler.message_queue) == 1
        assert handler.message_queue[0]["agent_id"] == "agent-q"

    def test_flush_queue_trims_to_max(self, handler):
        for i in range(20):
            handler.message_queue.append({"seq": i})
        handler.flush_queue(max_items=5)
        assert len(handler.message_queue) == 5

    def test_flush_queue_keeps_newest(self, handler):
        for i in range(10):
            handler.message_queue.append({"seq": i})
        handler.flush_queue(max_items=3)
        assert handler.message_queue[0]["seq"] == 7

    def test_encode_message_returns_json_string(self, handler):
        import json
        result = handler.encode_message({"type": "heartbeat", "ts": "2025-01-01"})
        assert isinstance(result, str)
        assert json.loads(result)["type"] == "heartbeat"

    def test_decode_message_parses_json(self, handler):
        result = handler.decode_message('{"type": "neural_sync", "sync_id": "xyz"}')
        assert result["type"] == "neural_sync"

    def test_decode_message_returns_none_on_invalid_json(self, handler):
        assert handler.decode_message("{ not valid json !!}") is None

    def test_queue_status_shape(self, handler):
        status = handler.get_queue_status()
        for key in ("queue_size", "protocol_version", "encryption_status", "total_handled"):
            assert key in status
        assert status["encryption_status"] == "NOT_IMPLEMENTED"

    def test_no_flush_needed_below_max(self, handler):
        for i in range(3):
            handler.message_queue.append({"seq": i})
        handler.flush_queue(max_items=500)
        assert len(handler.message_queue) == 3


# ---------------------------------------------------------------------------
# runtime.py
# ---------------------------------------------------------------------------

class TestRuntime:

    @pytest.fixture
    def runtime(self):
        from runtime import Runtime
        return Runtime(kernel=None, max_iterations=5)

    @pytest.mark.asyncio
    async def test_execute_agent_starts_task(self, runtime):
        await runtime.execute_agent("agent-r1", {"objective": "test"})
        assert "agent-r1" in runtime._tasks
        await runtime.terminate_agent("agent-r1", "test cleanup")

    @pytest.mark.asyncio
    async def test_get_execution_returns_record(self, runtime):
        await runtime.execute_agent("agent-r2", {"objective": "probe"})
        rec = runtime.get_execution("agent-r2")
        assert rec is not None
        assert rec.agent_id == "agent-r2"
        await runtime.terminate_agent("agent-r2", "test cleanup")

    @pytest.mark.asyncio
    async def test_get_execution_missing_returns_none(self, runtime):
        assert runtime.get_execution("ghost") is None

    @pytest.mark.asyncio
    async def test_duplicate_execute_is_noop(self, runtime):
        await runtime.execute_agent("agent-dup", {"objective": "task"})
        task1 = runtime._tasks["agent-dup"]
        await runtime.execute_agent("agent-dup", {"objective": "task again"})
        assert runtime._tasks["agent-dup"] is task1
        await runtime.terminate_agent("agent-dup", "test cleanup")

    @pytest.mark.asyncio
    async def test_terminate_agent_sets_halted(self, runtime):
        await runtime.execute_agent("agent-halt", {"objective": "run"})
        await runtime.terminate_agent("agent-halt", "manual")
        assert runtime.get_execution("agent-halt").status == "halted"

    @pytest.mark.asyncio
    async def test_terminate_nonexistent_is_noop(self, runtime):
        await runtime.terminate_agent("ghost", "cleanup")  # must not raise

    @pytest.mark.asyncio
    async def test_emergency_halt_stops_all_agents(self, runtime):
        await runtime.execute_agent("a1", {"objective": "task"})
        await runtime.execute_agent("a2", {"objective": "task"})
        await runtime.emergency_halt()
        for rec in runtime._executions.values():
            assert rec.status == "halted"

    @pytest.mark.asyncio
    async def test_tick_hook_is_called(self, runtime):
        hook_calls = []

        async def my_hook(agent_id, iteration, result):
            hook_calls.append((agent_id, iteration))

        runtime.register_tick_hook(my_hook)
        await runtime.execute_agent("hook-agent", {"objective": ""})
        await asyncio.sleep(0.1)
        assert len(hook_calls) >= 1
        assert hook_calls[0][0] == "hook-agent"

    @pytest.mark.asyncio
    async def test_agent_with_empty_objective_completes(self, runtime):
        await runtime.execute_agent("finish-fast", {"objective": ""})
        await asyncio.sleep(0.1)
        rec = runtime.get_execution("finish-fast")
        assert rec.status in ("completed", "halted")

    @pytest.mark.asyncio
    async def test_execution_stats_shape(self, runtime):
        await runtime.execute_agent("stat-agent", {"objective": ""})
        await asyncio.sleep(0.1)
        stats = runtime.get_execution_stats()
        for key in ("active_agents", "total_completed", "total_failed", "agents"):
            assert key in stats

    @pytest.mark.asyncio
    async def test_wait_for_agent_raises_without_task(self, runtime):
        with pytest.raises(ValueError, match="No running task"):
            await runtime.wait_for_agent("never-started", timeout=0.1)

    @pytest.mark.asyncio
    async def test_rogue_check_does_not_raise(self, runtime):
        from runtime import AgentExecution
        rec = AgentExecution(agent_id="rogue", config={})
        rec.iterations = 999
        rec.started_at = datetime.utcnow()
        await runtime._check_rogue_status("rogue", rec)  # must not raise

    @pytest.mark.asyncio
    async def test_max_iterations_triggers_halt(self):
        from runtime import Runtime
        rt = Runtime(kernel=None, max_iterations=2)
        await rt.execute_agent("iter-agent", {"objective": "never done"})
        await asyncio.sleep(0.3)
        rec = rt.get_execution("iter-agent")
        assert rec.status in ("halted", "failed")

    @pytest.mark.asyncio
    async def test_multiple_hooks_all_called(self, runtime):
        calls_a, calls_b = [], []

        async def hook_a(agent_id, iteration, result):
            calls_a.append(1)

        async def hook_b(agent_id, iteration, result):
            calls_b.append(1)

        runtime.register_tick_hook(hook_a)
        runtime.register_tick_hook(hook_b)
        await runtime.execute_agent("multi-hook", {"objective": ""})
        await asyncio.sleep(0.1)
        assert len(calls_a) >= 1
        assert len(calls_b) >= 1
