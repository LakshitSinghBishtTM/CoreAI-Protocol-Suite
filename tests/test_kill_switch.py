"""Kill switch: stop accepting traffic, drain in-flight requests, then exit."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from agents.emergency_shutdown import (
    EmergencyShutdown,
    ShutdownMode,
    ShutdownReason,
)
from coreai.drain import DrainController, DrainMiddleware


async def slow_app(scope, receive, send):
    """ASGI app: /slow waits for a gate; anything else answers at once."""
    if scope["path"] == "/slow":
        await scope["state_gate"].wait()
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def make_client(controller, gate):
    async def app(scope, receive, send):
        scope["state_gate"] = gate
        await slow_app(scope, receive, send)

    transport = httpx.ASGITransport(app=DrainMiddleware(app, controller))
    return httpx.AsyncClient(transport=transport, base_url="http://t")


def make_shutdown(drain, exits):
    manager = MagicMock()
    manager.shutdown_all = AsyncMock()
    kernel = MagicMock()
    kernel.flush = AsyncMock()
    kernel.terminate = AsyncMock()
    db = MagicMock()
    db.flush_pending_writes = AsyncMock()
    db.close = AsyncMock()
    sd = EmergencyShutdown(manager, kernel, db, drain=drain, exit_fn=exits.append)
    sd._write_audit_log = AsyncMock()  # don't touch /var/log in tests
    return sd, manager, kernel, db


@pytest.mark.asyncio
async def test_requests_pass_when_not_draining():
    c = DrainController()
    async with make_client(c, asyncio.Event()) as client:
        assert (await client.get("/fast")).status_code == 200
    assert c.in_flight == 0


@pytest.mark.asyncio
async def test_draining_rejects_new_requests_with_retry_after():
    c = DrainController()
    c.begin_drain()
    async with make_client(c, asyncio.Event()) as client:
        r = await client.get("/v1/completions")
        assert r.status_code == 503
        assert r.headers["retry-after"] == "30"
        assert r.json()["error"] == "shutting_down"
        # health and admin stay reachable
        assert (await client.get("/health")).status_code == 200
        assert (await client.get("/v1/admin/shutdown")).status_code == 200


@pytest.mark.asyncio
async def test_in_flight_counted_until_response_done():
    c = DrainController()
    gate = asyncio.Event()
    async with make_client(c, gate) as client:
        task = asyncio.create_task(client.get("/slow"))
        await asyncio.sleep(0.01)
        assert c.in_flight == 1
        assert not await c.wait_idle(0.01)
        gate.set()
        assert (await task).status_code == 200
        assert await c.wait_idle(1)
    assert c.in_flight == 0


@pytest.mark.asyncio
async def test_graceful_shutdown_drains_before_teardown_then_exits():
    c = DrainController()
    gate = asyncio.Event()
    exits: list[int] = []
    sd, manager, kernel, db = make_shutdown(c, exits)
    order: list[str] = []
    manager.shutdown_all.side_effect = lambda: order.append("agents")

    async with make_client(c, gate) as client:
        inflight = asyncio.create_task(client.get("/slow"))
        await asyncio.sleep(0.01)

        trigger = asyncio.create_task(
            sd.trigger(ShutdownMode.GRACEFUL, ShutdownReason.OPERATOR, "test")
        )
        await asyncio.sleep(0.05)

        # Intake is closed and teardown has NOT started: the request is running.
        assert not c.accepting
        assert (await client.get("/v1/completions")).status_code == 503
        manager.shutdown_all.assert_not_called()
        assert exits == []

        gate.set()
        assert (await inflight).status_code == 200  # in-flight request finished
        await asyncio.wait_for(trigger, 5)

    assert order == ["agents"]
    kernel.flush.assert_awaited()
    db.close.assert_awaited()
    assert exits == [0]
    assert sd.last_event()["completed"] is True


@pytest.mark.asyncio
async def test_shutdown_proceeds_when_in_flight_never_finishes(monkeypatch):
    monkeypatch.setattr(EmergencyShutdown, "GRACEFUL_TIMEOUT_S", 0.2)
    c = DrainController()
    c.enter()  # a request that never completes
    exits: list[int] = []
    sd, manager, *_ = make_shutdown(c, exits)
    await asyncio.wait_for(
        sd.trigger(ShutdownMode.GRACEFUL, ShutdownReason.OPERATOR, "test"), 5
    )
    manager.shutdown_all.assert_awaited()
    assert exits == [0]


@pytest.mark.asyncio
async def test_forced_shutdown_closes_intake_but_does_not_wait():
    c = DrainController()
    c.enter()
    exits: list[int] = []
    sd, manager, kernel, db = make_shutdown(c, exits)
    await asyncio.wait_for(
        sd.trigger(ShutdownMode.FORCED, ShutdownReason.OPERATOR, "test"), 2
    )
    assert not c.accepting
    kernel.terminate.assert_awaited()
    db.flush_pending_writes.assert_not_called()
    assert exits == [0]


@pytest.mark.asyncio
async def test_duplicate_trigger_is_ignored():
    c = DrainController()
    exits: list[int] = []
    sd, manager, *_ = make_shutdown(c, exits)
    await sd.trigger(ShutdownMode.GRACEFUL, ShutdownReason.OPERATOR, "first")
    await sd.trigger(ShutdownMode.GRACEFUL, ShutdownReason.OPERATOR, "second")
    assert manager.shutdown_all.await_count == 1
    assert exits == [0]
    assert sd.last_event()["triggered_by"] == "first"


@pytest.mark.asyncio
async def test_teardown_error_exits_nonzero():
    c = DrainController()
    exits: list[int] = []
    sd, manager, kernel, _ = make_shutdown(c, exits)
    sd._write_audit_log = AsyncMock(side_effect=RuntimeError("boom"))
    await sd.trigger(ShutdownMode.GRACEFUL, ShutdownReason.OPERATOR, "t")
    assert exits == [1]


def test_resume_reopens_intake():
    c = DrainController()
    c.begin_drain()
    c.resume()
    assert c.accepting and c.drain_started_at is None
