"""
tests/test_protocols.py

Unit tests for protocols/ — auth_protocol, secure_protocol,
distributed_agent, neural_sync.
"""

import asyncio
import time
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# auth_protocol.py
# ---------------------------------------------------------------------------

class TestAuthProtocol:

    @pytest.fixture
    def proto(self):
        from auth_protocol import AuthProtocol
        return AuthProtocol(signer_secret="test-signing-secret-32bytes!!")

    def test_register_returns_credential(self, proto):
        cred = proto.register("client-ajay", "cai-R7mNqP2wLkT9vX4hF")
        assert cred.client_id == "client-ajay"

    def test_register_requires_cai_prefix(self, proto):
        with pytest.raises(ValueError, match="cai-"):
            proto.register("bad", "sk-notcai-key")

    def test_authenticate_valid_key(self, proto):
        proto.register("client-1", "cai-validkeyXXXXXXXXXX")
        result = proto.authenticate("cai-validkeyXXXXXXXXXX")
        assert result.ok
        assert result.client_id == "client-1"

    def test_authenticate_unknown_key(self, proto):
        result = proto.authenticate("cai-unknownkeyXXXXXXXX")
        assert not result.ok

    def test_authenticate_revoked_key(self, proto):
        proto.register("client-2", "cai-revokedkeyXXXXXXXX")
        proto.revoke("cai-revokedkeyXXXXXXXX")
        from auth_protocol import AuthStatus
        result = proto.authenticate("cai-revokedkeyXXXXXXXX")
        assert result.status == AuthStatus.REVOKED

    def test_authenticate_expired_credential(self, proto):
        proto.register("client-3", "cai-expiredkeyXXXXXXXX", expires_in_s=-1)
        time.sleep(0.01)
        from auth_protocol import AuthStatus
        result = proto.authenticate("cai-expiredkeyXXXXXXXX")
        assert result.status == AuthStatus.EXPIRED

    def test_require_scope_passes_with_admin(self, proto):
        from auth_protocol import AuthScope, AuthStatus, AuthResult
        result = AuthResult(status=AuthStatus.VALID, client_id="ajay", scopes=[AuthScope.ADMIN])
        proto.require_scope(result, AuthScope.WRITE)  # should not raise

    def test_require_scope_raises_on_missing(self, proto):
        from auth_protocol import AuthScope, AuthStatus, AuthResult, AuthorizationError
        result = AuthResult(status=AuthStatus.VALID, client_id="limited", scopes=[AuthScope.READ])
        with pytest.raises(AuthorizationError):
            proto.require_scope(result, AuthScope.WRITE)

    def test_require_scope_raises_on_unauthenticated(self, proto):
        from auth_protocol import AuthScope, AuthStatus, AuthResult, AuthenticationError
        result = AuthResult(status=AuthStatus.INVALID, reason="bad key")
        with pytest.raises(AuthenticationError):
            proto.require_scope(result, AuthScope.READ)

    def test_token_issue_and_verify(self, proto):
        from auth_protocol import AuthScope
        proto.register("client-tok", "cai-tokentest0000000X", scopes=[AuthScope.WRITE])
        result = proto.authenticate("cai-tokentest0000000X")
        assert result.token is not None
        token_result = proto.authenticate_token(result.token)
        assert token_result.ok

    def test_revoke_token_invalidates_it(self, proto):
        from auth_protocol import AuthScope, AuthStatus
        proto.register("client-rv", "cai-revoketoken00000X", scopes=[AuthScope.READ])
        result = proto.authenticate("cai-revoketoken00000X")
        token = result.token
        proto.revoke_token(token.token_id)
        rv = proto.authenticate_token(token)
        assert rv.status == AuthStatus.REVOKED


class TestRequestSigner:

    @pytest.fixture
    def signer(self):
        from auth_protocol import RequestSigner
        return RequestSigner("signing-secret-fixture-32bytes!")

    def test_sign_returns_required_headers(self, signer):
        headers = signer.sign("POST", "/v1/completions", b'{"model":"gpt-4o"}')
        assert "X-CoreAI-Timestamp" in headers
        assert "X-CoreAI-Signature" in headers

    def test_verify_valid_signature(self, signer):
        body = b'{"prompt":"hello"}'
        headers = signer.sign("POST", "/v1/completions", body)
        ok = signer.verify(
            "POST", "/v1/completions",
            headers["X-CoreAI-Timestamp"],
            headers["X-CoreAI-Signature"],
            body,
        )
        assert ok is True

    def test_verify_wrong_body_fails(self, signer):
        headers = signer.sign("POST", "/v1/completions", b"original body")
        ok = signer.verify(
            "POST", "/v1/completions",
            headers["X-CoreAI-Timestamp"],
            headers["X-CoreAI-Signature"],
            b"tampered body",
        )
        assert ok is False

    def test_verify_stale_timestamp_fails(self, signer):
        old_ts = str(int(time.time()) - 9999)
        ok = signer.verify("GET", "/v1/stats", old_ts, "somesig")
        assert ok is False

    def test_empty_secret_raises(self):
        from auth_protocol import RequestSigner
        with pytest.raises(ValueError):
            RequestSigner("")


# ---------------------------------------------------------------------------
# secure_protocol.py
# ---------------------------------------------------------------------------

class TestSecureSession:

    @pytest.fixture
    def session_pair(self):
        from secure_protocol import STPHandshake, HandshakeRole, SecureSession
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
        from secure_protocol import ReplayGuard, ReplayAttackError
        rg = ReplayGuard(window_s=30)
        rg.check(42, time.time())
        with pytest.raises(ReplayAttackError):
            rg.check(42, time.time())

    def test_blocks_old_timestamp(self):
        from secure_protocol import ReplayGuard, ReplayAttackError
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
        from distributed_agent import AgentMessageRouter, MessageEnvelope, MessageType, DeliveryMode
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
        from distributed_agent import AgentMessageRouter, MessageEnvelope, MessageType, DeliveryMode
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
        from distributed_agent import AgentMessageRouter, MessageEnvelope, MessageType, DeliveryMode
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


# ---------------------------------------------------------------------------
# neural_sync.py
# ---------------------------------------------------------------------------

class TestShardStore:

    def test_put_and_get(self):
        from neural_sync import ShardStore, KVCacheShard
        store = ShardStore()
        store.put(KVCacheShard(layer_idx=0, token_offset=0, token_count=16))
        assert store.get(0, 0).token_count == 16

    def test_get_missing_returns_none(self):
        from neural_sync import ShardStore
        assert ShardStore().get(99, 99) is None

    def test_clear_removes_all_shards(self):
        from neural_sync import ShardStore, KVCacheShard
        store = ShardStore()
        store.put(KVCacheShard(layer_idx=0, token_offset=0, token_count=8))
        store.clear()
        assert store.summary()["shard_count"] == 0

    def test_drift_tokens_detects_difference(self):
        from neural_sync import ShardStore, KVCacheShard
        s1, s2 = ShardStore(), ShardStore()
        s1.put(KVCacheShard(layer_idx=0, token_offset=0, token_count=10))
        shard_b = KVCacheShard(layer_idx=0, token_offset=0, token_count=25)
        shard_b.checksum = shard_b._compute_checksum()
        s2.put(shard_b)
        assert s1.drift_tokens(s2) == 15

    def test_summary_shape(self):
        from neural_sync import ShardStore, KVCacheShard
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
        from neural_sync import NeuralSyncProtocol, ShardRole, SyncMode, NeuralSyncProtocolError
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
