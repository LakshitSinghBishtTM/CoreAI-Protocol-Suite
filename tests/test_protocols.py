"""
tests/test_protocols.py

Unit tests for protocols/ — secure_protocol, distributed_agent,
neural_sync.
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


# ---------------------------------------------------------------------------
# neural_sync.py
# ---------------------------------------------------------------------------


class TestShardStore:

    def test_put_and_get(self):
        from neural_sync import KVCacheShard, ShardStore

        store = ShardStore()
        store.put(KVCacheShard(layer_idx=0, token_offset=0, token_count=16))
        assert store.get(0, 0).token_count == 16

    def test_get_missing_returns_none(self):
        from neural_sync import ShardStore

        assert ShardStore().get(99, 99) is None

    def test_clear_removes_all_shards(self):
        from neural_sync import KVCacheShard, ShardStore

        store = ShardStore()
        store.put(KVCacheShard(layer_idx=0, token_offset=0, token_count=8))
        store.clear()
        assert store.summary()["shard_count"] == 0

    def test_drift_tokens_detects_difference(self):
        from neural_sync import KVCacheShard, ShardStore

        s1, s2 = ShardStore(), ShardStore()
        s1.put(KVCacheShard(layer_idx=0, token_offset=0, token_count=10))
        shard_b = KVCacheShard(layer_idx=0, token_offset=0, token_count=25)
        shard_b.checksum = shard_b._compute_checksum()
        s2.put(shard_b)
        assert s1.drift_tokens(s2) == 15

    def test_summary_shape(self):
        from neural_sync import KVCacheShard, ShardStore

        store = ShardStore()
        store.put(KVCacheShard(layer_idx=2, token_offset=0, token_count=5))
        s = store.summary()
        assert "shard_count" in s
        assert "total_tokens" in s
        assert "layers" in s


class TestNeuralSyncProtocol:

    @pytest.mark.asyncio
    async def test_primary_can_publish(self):
        from neural_sync import NeuralSyncProtocol, ShardRole, SyncMode

        primary = NeuralSyncProtocol("primary-node", role=ShardRole.PRIMARY)
        replica = NeuralSyncProtocol("replica-node", role=ShardRole.REPLICA)
        primary.connect_peer(replica)
        await primary.start()
        await primary.publish(primary.build_frame(SyncMode.FULL))
        received = await replica.receive(timeout_s=0.5)
        assert received is not None
        assert received.source_node == "primary-node"
        await primary.stop()

    @pytest.mark.asyncio
    async def test_replica_cannot_publish(self):
        from neural_sync import (
            NeuralSyncProtocol,
            NeuralSyncProtocolError,
            ShardRole,
            SyncMode,
        )

        replica = NeuralSyncProtocol("r", role=ShardRole.REPLICA)
        frame = replica.build_frame(SyncMode.DELTA)
        with pytest.raises(NeuralSyncProtocolError):
            await replica.publish(frame)

    @pytest.mark.asyncio
    async def test_apply_full_frame_sets_in_sync(self):
        from neural_sync import NeuralSyncProtocol, ShardRole, SyncMode, SyncStatus

        primary = NeuralSyncProtocol("p", role=ShardRole.PRIMARY)
        replica = NeuralSyncProtocol("r", role=ShardRole.REPLICA)
        status = replica.apply_frame(primary.build_frame(SyncMode.FULL))
        assert status == SyncStatus.IN_SYNC

    def test_stats_contains_expected_keys(self):
        from neural_sync import NeuralSyncProtocol, ShardRole

        stats = NeuralSyncProtocol("n", role=ShardRole.REPLICA).get_stats()
        for key in ("node_id", "role", "frames_sent", "frames_received", "store"):
            assert key in stats

    def test_connect_and_disconnect_peer(self):
        from neural_sync import NeuralSyncProtocol, ShardRole

        a = NeuralSyncProtocol("a", ShardRole.PRIMARY)
        b = NeuralSyncProtocol("b", ShardRole.REPLICA)
        a.connect_peer(b)
        assert "b" in a._peers
        a.disconnect_peer("b")
        assert "b" not in a._peers
