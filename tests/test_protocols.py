"""
tests/test_protocols.py

Unit tests for protocols/ — secure_protocol, distributed_agent.
"""

import asyncio
import time
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# secure_protocol.py
# ---------------------------------------------------------------------------


class TestSecureSession:

    @pytest.fixture
    def session_pair(self):
        from secure_protocol import HandshakeRole, SecureSession, STPHandshake

        shared = b"shared-secret-for-testing-32byt"
        init = STPHandshake("node-init", HandshakeRole.INITIATOR)
        resp = STPHandshake("node-resp", HandshakeRole.RESPONDER)
        hello_i = init.build_hello()
        hello_r = resp.build_hello()
        init.receive_hello(hello_r)
        resp.receive_hello(hello_i)
        keys_i = init.derive_keys(shared)
        keys_r = resp.derive_keys(shared)
        return SecureSession("node-init", keys_i), SecureSession("node-resp", keys_r)

    def test_send_returns_bytes(self, session_pair):
        sess_i, _ = session_pair
        assert isinstance(sess_i.send(b"hello peer"), bytes)

    def test_roundtrip(self, session_pair):
        sess_i, sess_r = session_pair
        plaintext = b"CoreAI handshake test payload"
        assert sess_r.receive(sess_i.send(plaintext)) == plaintext

    def test_tampered_frame_raises(self, session_pair):
        from secure_protocol import MACVerificationError

        sess_i, sess_r = session_pair
        frame = bytearray(sess_i.send(b"sensitive data"))
        frame[45] ^= 0xFF
        with pytest.raises(MACVerificationError):
            sess_r.receive(bytes(frame))

    def test_stats_tracks_bytes_sent(self, session_pair):
        sess_i, _ = session_pair
        sess_i.send(b"A" * 100)
        assert sess_i.get_stats()["bytes_sent"] > 0

    def test_send_after_close_raises(self, session_pair):
        from secure_protocol import SessionNotEstablishedError

        sess_i, _ = session_pair
        sess_i.close()
        with pytest.raises(SessionNotEstablishedError):
            sess_i.send(b"too late")


class TestReplayGuard:

    def test_allows_first_occurrence(self):
        from secure_protocol import ReplayGuard

        ReplayGuard(window_s=30).check(1, time.time())

    def test_blocks_duplicate_sequence(self):
        from secure_protocol import ReplayAttackError, ReplayGuard

        rg = ReplayGuard(window_s=30)
        rg.check(42, time.time())
        with pytest.raises(ReplayAttackError):
            rg.check(42, time.time())

    def test_blocks_old_timestamp(self):
        from secure_protocol import ReplayAttackError, ReplayGuard

        rg = ReplayGuard(window_s=30)
        with pytest.raises(ReplayAttackError):
            rg.check(99, time.time() - 9999)

    def test_reset_clears_seen(self):
        from secure_protocol import ReplayGuard

        rg = ReplayGuard(window_s=30)
        rg.check(7, time.time())
        rg.reset()
        rg.check(7, time.time())  # should not raise after reset


# ---------------------------------------------------------------------------
# distributed_agent.py
# ---------------------------------------------------------------------------


class TestAgentMessageRouter:

    @pytest.mark.asyncio
    async def test_register_creates_queue(self):
        from distributed_agent import AgentMessageRouter

        q = AgentMessageRouter().register_agent("agent-a")
        assert q is not None

    @pytest.mark.asyncio
    async def test_unicast_delivers_to_recipient(self):
        from distributed_agent import (
            AgentMessageRouter,
            DeliveryMode,
            MessageEnvelope,
            MessageType,
        )

        router = AgentMessageRouter()
        router.register_agent("agent-a")
        router.register_agent("agent-b")
        env = MessageEnvelope(
            msg_type=MessageType.HEARTBEAT,
            sender_id="agent-a",
            recipient_id="agent-b",
            delivery_mode=DeliveryMode.AT_MOST_ONCE,
            ttl_s=60,
        )
        await router.send(env)
        assert not router._queues["agent-b"].empty()

    @pytest.mark.asyncio
    async def test_expired_message_dropped(self):
        from distributed_agent import (
            AgentMessageRouter,
            DeliveryMode,
            MessageEnvelope,
            MessageType,
        )

        router = AgentMessageRouter()
        router.register_agent("agent-x")
        env = MessageEnvelope(
            msg_type=MessageType.HEARTBEAT,
            sender_id="other",
            recipient_id="agent-x",
            delivery_mode=DeliveryMode.AT_MOST_ONCE,
            ttl_s=0.0,
            created_at=time.time() - 999,
        )
        await router.send(env)
        assert router._queues["agent-x"].empty()

    @pytest.mark.asyncio
    async def test_exactly_once_dedup(self):
        from distributed_agent import (
            AgentMessageRouter,
            DeliveryMode,
            MessageEnvelope,
            MessageType,
        )

        router = AgentMessageRouter()
        router.register_agent("agent-d")
        env = MessageEnvelope(
            msg_type=MessageType.HEARTBEAT,
            sender_id="src",
            recipient_id="agent-d",
            delivery_mode=DeliveryMode.EXACTLY_ONCE,
            ttl_s=60,
        )
        await router.send(env)
        await router.send(env)  # duplicate
        assert router._queues["agent-d"].qsize() == 1

    @pytest.mark.asyncio
    async def test_round_trip_through_protocol_preserves_payload(self):
        from distributed_agent import DeliveryMode, DistributedAgentProtocol

        protocol = DistributedAgentProtocol()
        protocol.register_agent("agent-x", capabilities=[])
        await protocol.send_task(
            sender_id="orchestrator",
            recipient_id="agent-x",
            task_payload={"action": "run_tool", "tool": "search"},
            delivery=DeliveryMode.AT_MOST_ONCE,
        )
        received = await protocol.receive("agent-x", timeout_s=1.0)
        assert received is not None
        assert received.payload == {"action": "run_tool", "tool": "search"}

    @pytest.mark.asyncio
    async def test_tampered_message_rejected(self):
        from distributed_agent import (
            AgentMessageRouter,
            DeliveryMode,
            MessageEnvelope,
            MessageType,
        )

        router = AgentMessageRouter()
        router.register_agent("agent-a")
        router.register_agent("agent-b")
        env = MessageEnvelope(
            msg_type=MessageType.HEARTBEAT,
            sender_id="agent-a",
            recipient_id="agent-b",
            delivery_mode=DeliveryMode.AT_MOST_ONCE,
            ttl_s=60,
        )
        await router.send(env)
        sealed = router._queues["agent-b"].get_nowait()
        tampered = sealed[:-1] + bytes([sealed[-1] ^ 0xFF])  # flip a MAC byte

        assert router.unseal("agent-b", tampered) is None
        assert router.unseal("agent-b", sealed) is not None  # untouched copy still opens

    @pytest.mark.asyncio
    async def test_broadcast_to_multiple_recipients_all_open(self):
        # Regression test: sessions are keyed per recipient specifically so that
        # broadcasting to several agents doesn't make the second recipient's
        # unseal() look like a replay of the first's.
        from distributed_agent import (
            AgentMessageRouter,
            DeliveryMode,
            MessageEnvelope,
            MessageType,
        )

        router = AgentMessageRouter()
        router.register_agent("agent-a")
        router.register_agent("agent-b")
        router.register_agent("agent-c")
        env = MessageEnvelope(
            msg_type=MessageType.HEARTBEAT,
            sender_id="agent-a",
            recipient_id="",  # broadcast
            delivery_mode=DeliveryMode.AT_MOST_ONCE,
            ttl_s=60,
        )
        await router.send(env)

        opened_b = router.unseal("agent-b", router._queues["agent-b"].get_nowait())
        opened_c = router.unseal("agent-c", router._queues["agent-c"].get_nowait())

        assert opened_b is not None
        assert opened_c is not None
        assert opened_b.msg_id == opened_c.msg_id == env.msg_id

