"""
protocols/neural_sync.py

CoreAI Neural Synchronisation Protocol (NSP).
Manages state synchronisation between inference nodes, shared KV-cache
replication, and attention-head alignment across distributed model shards.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NSP_VERSION = "nsp/0.9-experimental"
SYNC_INTERVAL_S = 0.25  # 250ms target sync cadence
CACHE_SHARD_SIZE = 512  # token slots per shard
MAX_DRIFT_TOKENS = 64  # tolerated KV-cache drift before hard resync
VECTOR_DIM = 4096  # hidden state dimensionality (default llama-class)
CHECKSUM_WINDOW = 32  # tokens included in rolling checksum


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class SyncMode(str, Enum):
    FULL = "full"  # complete state transfer
    DELTA = "delta"  # diff-only transfer
    CHECKSUM = "checksum"  # verify only, no data transfer


class SyncStatus(str, Enum):
    IN_SYNC = "in_sync"
    DRIFTED = "drifted"
    DIVERGED = "diverged"
    SYNCING = "syncing"
    UNKNOWN = "unknown"


class ShardRole(str, Enum):
    PRIMARY = "primary"
    REPLICA = "replica"
    OBSERVER = "observer"


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class KVCacheShard:
    """
    Represents a slice of a KV-cache from a single transformer layer.
    Keys and values are stored as flat float lists (serialisable over wire).
    In production these would be torch.Tensor objects.
    """

    shard_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    layer_idx: int = 0
    token_offset: int = 0
    token_count: int = 0
    keys: list[list[float]] = field(default_factory=list)  # [seq_len, head_dim]
    values: list[list[float]] = field(default_factory=list)  # [seq_len, head_dim]
    created_at: float = field(default_factory=time.time)
    checksum: str = ""

    def __post_init__(self):
        if not self.checksum:
            self.checksum = self._compute_checksum()

    def _compute_checksum(self) -> str:
        # Hash the token count + first/last key vectors as a fast integrity check
        probe = str(self.token_count)
        if self.keys:
            probe += str(self.keys[0]) + str(self.keys[-1])
        return hashlib.sha256(probe.encode()).hexdigest()[:12]

    def is_compatible_with(self, other: "KVCacheShard") -> bool:
        return (
            self.layer_idx == other.layer_idx
            and self.token_offset == other.token_offset
        )

    def diff(self, other: "KVCacheShard") -> Optional["KVCacheShardDelta"]:
        if self.checksum == other.checksum:
            return None
        new_token_count = other.token_count - self.token_count
        if new_token_count <= 0:
            return None
        return KVCacheShardDelta(
            base_shard_id=self.shard_id,
            layer_idx=self.layer_idx,
            new_keys=other.keys[self.token_count :],
            new_values=other.values[self.token_count :],
            base_checksum=self.checksum,
            target_checksum=other.checksum,
        )


@dataclass
class KVCacheShardDelta:
    base_shard_id: str
    layer_idx: int
    new_keys: list[list[float]]
    new_values: list[list[float]]
    base_checksum: str
    target_checksum: str
    created_at: float = field(default_factory=time.time)

    @property
    def token_delta(self) -> int:
        return len(self.new_keys)


@dataclass
class HiddenState:
    """Serialisable snapshot of a model's hidden state at a given token position."""

    state_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    token_position: int = 0
    layer_idx: int = 0
    vector: list[float] = field(default_factory=list)  # shape: [hidden_dim]
    norm_factor: float = 1.0
    captured_at: float = field(default_factory=time.time)

    @property
    def magnitude(self) -> float:
        if not self.vector:
            return 0.0
        return sum(x * x for x in self.vector) ** 0.5


@dataclass
class SyncFrame:
    """A single synchronisation frame exchanged between nodes."""

    frame_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    source_node: str = ""
    target_node: str = ""  # empty = broadcast
    mode: SyncMode = SyncMode.DELTA
    sequence_no: int = 0
    shards: list[KVCacheShard] = field(default_factory=list)
    deltas: list[KVCacheShardDelta] = field(default_factory=list)
    hidden_states: list[HiddenState] = field(default_factory=list)
    frame_checksum: str = ""
    created_at: float = field(default_factory=time.time)

    def __post_init__(self):
        if not self.frame_checksum:
            self.frame_checksum = self._compute_frame_checksum()

    def _compute_frame_checksum(self) -> str:
        probe = f"{self.frame_id}{self.sequence_no}{len(self.shards)}{len(self.deltas)}"
        return hashlib.sha256(probe.encode()).hexdigest()[:16]

    def verify(self) -> bool:
        return self.frame_checksum == self._compute_frame_checksum()


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SyncDivergenceError(RuntimeError):
    """Raised when KV-cache drift exceeds MAX_DRIFT_TOKENS and full resync is required."""


class ShardChecksumMismatch(RuntimeError):
    """Raised when a received shard fails its integrity check."""


class NeuralSyncProtocolError(RuntimeError):
    """General NSP protocol violation."""


# ---------------------------------------------------------------------------
# Shard store
# ---------------------------------------------------------------------------


class ShardStore:
    """
    In-memory store for KV-cache shards.
    In production this would be backed by pinned GPU memory or shared memory.
    """

    def __init__(self):
        self._shards: dict[str, KVCacheShard] = {}  # key: f"{layer_idx}:{token_offset}"

    def _key(self, layer_idx: int, token_offset: int) -> str:
        return f"{layer_idx}:{token_offset}"

    def put(self, shard: KVCacheShard) -> None:
        k = self._key(shard.layer_idx, shard.token_offset)
        self._shards[k] = shard

    def get(self, layer_idx: int, token_offset: int) -> Optional[KVCacheShard]:
        return self._shards.get(self._key(layer_idx, token_offset))

    def all_shards(self) -> list[KVCacheShard]:
        return list(self._shards.values())

    def apply_delta(self, delta: KVCacheShardDelta) -> bool:
        """Apply an incremental delta to an existing shard. Returns True if successful."""
        target = next(
            (s for s in self._shards.values() if s.shard_id == delta.base_shard_id),
            None,
        )
        if target is None:
            logger.warning("Base shard %s not found for delta", delta.base_shard_id)
            return False

        if target.checksum != delta.base_checksum:
            logger.error(
                "Shard %s checksum mismatch — expected %s got %s",
                delta.base_shard_id,
                delta.base_checksum,
                target.checksum,
            )
            return False

        target.keys.extend(delta.new_keys)
        target.values.extend(delta.new_values)
        target.token_count += delta.token_delta
        target.checksum = delta.target_checksum
        return True

    def drift_tokens(self, other: "ShardStore") -> int:
        """Returns the maximum token count difference across matching shards."""
        max_drift = 0
        for shard in self.all_shards():
            peer = other.get(shard.layer_idx, shard.token_offset)
            if peer:
                drift = abs(shard.token_count - peer.token_count)
                max_drift = max(max_drift, drift)
        return max_drift

    def clear(self) -> None:
        self._shards.clear()

    def summary(self) -> dict:
        shards = self.all_shards()
        return {
            "shard_count": len(shards),
            "total_tokens": sum(s.token_count for s in shards),
            "layers": sorted({s.layer_idx for s in shards}),
        }


# ---------------------------------------------------------------------------
# Neural Sync Protocol
# ---------------------------------------------------------------------------


class NeuralSyncProtocol:
    """
    Manages KV-cache and hidden-state synchronisation between CoreAI inference nodes.

    Each node has a role (PRIMARY / REPLICA / OBSERVER):
    - PRIMARY: source of truth, generates sync frames
    - REPLICA: applies incoming frames, serves reads
    - OBSERVER: receives frames but does not apply (monitoring only)

    Usage::

        nsp = NeuralSyncProtocol(node_id="node-a", role=ShardRole.PRIMARY)
        await nsp.start()

        # On primary: publish a frame
        frame = nsp.build_frame(mode=SyncMode.DELTA)
        await nsp.publish(frame)

        # On replica: receive and apply
        frame = await nsp.receive()
        nsp.apply_frame(frame)
    """

    def __init__(
        self,
        node_id: str,
        role: ShardRole = ShardRole.REPLICA,
        sync_interval_s: float = SYNC_INTERVAL_S,
    ):
        self.node_id = node_id
        self.role = role
        self.sync_interval_s = sync_interval_s
        self.store = ShardStore()
        self._peers: dict[str, "NeuralSyncProtocol"] = {}
        self._sequence_no = 0
        self._status = SyncStatus.UNKNOWN
        self._running = False
        self._sync_task: Optional[asyncio.Task] = None
        self._inbox: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._stats = {
            "frames_sent": 0,
            "frames_received": 0,
            "deltas_applied": 0,
            "full_resyncs": 0,
            "checksum_failures": 0,
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._running = True
        if self.role == ShardRole.PRIMARY:
            self._sync_task = asyncio.create_task(self._sync_loop())
        logger.info(
            "NeuralSyncProtocol started: node=%s role=%s", self.node_id, self.role
        )

    async def stop(self) -> None:
        self._running = False
        if self._sync_task:
            self._sync_task.cancel()
            try:
                await self._sync_task
            except asyncio.CancelledError:
                pass
        logger.info("NeuralSyncProtocol stopped: node=%s", self.node_id)

    # ------------------------------------------------------------------
    # Peer management
    # ------------------------------------------------------------------

    def connect_peer(self, peer: "NeuralSyncProtocol") -> None:
        self._peers[peer.node_id] = peer
        logger.debug("NSP peer connected: %s → %s", self.node_id, peer.node_id)

    def disconnect_peer(self, node_id: str) -> None:
        self._peers.pop(node_id, None)

    # ------------------------------------------------------------------
    # Frame construction
    # ------------------------------------------------------------------

    def build_frame(self, mode: SyncMode = SyncMode.DELTA) -> SyncFrame:
        self._sequence_no += 1
        shards: list[KVCacheShard] = []
        deltas: list[KVCacheShardDelta] = []

        if mode == SyncMode.FULL:
            shards = self.store.all_shards()
        elif mode == SyncMode.DELTA:
            # In a real implementation, deltas would be computed against each
            # replica's last acknowledged sequence number.
            shards = []
            deltas = []

        return SyncFrame(
            source_node=self.node_id,
            mode=mode,
            sequence_no=self._sequence_no,
            shards=shards,
            deltas=deltas,
        )

    # ------------------------------------------------------------------
    # Publish / receive
    # ------------------------------------------------------------------

    async def publish(self, frame: SyncFrame) -> None:
        if self.role != ShardRole.PRIMARY:
            raise NeuralSyncProtocolError("Only PRIMARY nodes may publish sync frames")

        if not frame.verify():
            raise NeuralSyncProtocolError(f"Frame {frame.frame_id[:8]} failed checksum")

        for peer in self._peers.values():
            try:
                await peer._inbox.put(frame)
            except asyncio.QueueFull:
                logger.warning(
                    "NSP inbox full for peer %s — frame dropped", peer.node_id
                )

        self._stats["frames_sent"] += 1

    async def receive(self, timeout_s: float = 1.0) -> Optional[SyncFrame]:
        try:
            return await asyncio.wait_for(self._inbox.get(), timeout=timeout_s)
        except asyncio.TimeoutError:
            return None

    # ------------------------------------------------------------------
    # Apply
    # ------------------------------------------------------------------

    def apply_frame(self, frame: SyncFrame) -> SyncStatus:
        if self.role == ShardRole.OBSERVER:
            self._stats["frames_received"] += 1
            return SyncStatus.IN_SYNC

        if not frame.verify():
            self._stats["checksum_failures"] += 1
            raise ShardChecksumMismatch(
                f"Frame {frame.frame_id[:8]} failed integrity check"
            )

        self._stats["frames_received"] += 1

        if frame.mode == SyncMode.FULL:
            self.store.clear()
            for shard in frame.shards:
                self.store.put(shard)
            self._stats["full_resyncs"] += 1
            self._status = SyncStatus.IN_SYNC

        elif frame.mode == SyncMode.DELTA:
            for delta in frame.deltas:
                ok = self.store.apply_delta(delta)
                if ok:
                    self._stats["deltas_applied"] += 1
                else:
                    self._stats["checksum_failures"] += 1
                    self._status = SyncStatus.DRIFTED
                    return SyncStatus.DRIFTED

            for shard in frame.shards:
                self.store.put(shard)

            self._status = SyncStatus.IN_SYNC

        elif frame.mode == SyncMode.CHECKSUM:
            # Verify local checksums against frame — no data transfer
            for shard in frame.shards:
                local = self.store.get(shard.layer_idx, shard.token_offset)
                if local and local.checksum != shard.checksum:
                    self._status = SyncStatus.DRIFTED
                    return SyncStatus.DRIFTED
            self._status = SyncStatus.IN_SYNC

        return self._status

    def check_drift(self, peer: "NeuralSyncProtocol") -> int:
        return self.store.drift_tokens(peer.store)

    # ------------------------------------------------------------------
    # Sync loop (PRIMARY only)
    # ------------------------------------------------------------------

    async def _sync_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self.sync_interval_s)
            if not self._peers:
                continue
            try:
                frame = self.build_frame(mode=SyncMode.DELTA)
                await self.publish(frame)
            except Exception as exc:  # pylint: disable=broad-except
                logger.error("NSP sync loop error: %s", exc)

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def get_stats(self) -> dict:
        return {
            "node_id": self.node_id,
            "role": self.role,
            "status": self._status,
            "sequence_no": self._sequence_no,
            "peers": list(self._peers.keys()),
            "store": self.store.summary(),
            **self._stats,
        }
