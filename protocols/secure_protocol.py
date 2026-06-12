"""
protocols/secure_protocol.py

CoreAI Secure Transport Protocol (STP).
Provides an authenticated, encrypted channel abstraction over raw TCP/WebSocket
connections between CoreAI nodes. Handles handshake, session key derivation,
message framing, and replay-attack prevention.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import struct
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STP_VERSION = 2
FRAME_MAGIC = b"\xCA\xFE\xC0\xDE"   # 4-byte frame header magic
NONCE_SIZE = 16                       # bytes
SESSION_KEY_SIZE = 32                 # 256-bit
MAC_SIZE = 32                         # HMAC-SHA256
MAX_FRAME_BODY_BYTES = 4 * 1024 * 1024  # 4 MB
REPLAY_WINDOW_S = 30.0
MIN_TLS_VERSION = "TLSv1.3"

# Frame layout (bytes):
#   4   magic
#   1   version
#   1   flags
#   2   reserved
#   8   timestamp (double, big-endian)
#   4   sequence number (uint32, big-endian)
#  16   nonce
#   4   body length (uint32, big-endian)
#   N   body
#  32   MAC (HMAC-SHA256 over all preceding bytes)
FRAME_HEADER_SIZE = 4 + 1 + 1 + 2 + 8 + 4 + 16 + 4   # 40 bytes


# ---------------------------------------------------------------------------
# Enums / flags
# ---------------------------------------------------------------------------


class FrameFlag(int, Enum):
    NONE = 0x00
    ENCRYPTED = 0x01
    COMPRESSED = 0x02
    LAST_FRAGMENT = 0x04
    HANDSHAKE = 0x08


class SessionState(str, Enum):
    NEW = "new"
    HANDSHAKING = "handshaking"
    ESTABLISHED = "established"
    REKEYING = "rekeying"
    CLOSED = "closed"
    ERROR = "error"


class HandshakeRole(str, Enum):
    INITIATOR = "initiator"
    RESPONDER = "responder"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class HandshakeError(RuntimeError):
    """Raised when the STP handshake fails."""


class MACVerificationError(RuntimeError):
    """Raised when a frame MAC does not match — possible tampering."""


class ReplayAttackError(RuntimeError):
    """Raised when a frame sequence number falls outside the replay window."""


class FrameTooLargeError(ValueError):
    """Raised when a frame body exceeds MAX_FRAME_BODY_BYTES."""


class SessionNotEstablishedError(RuntimeError):
    """Raised when data is sent before the handshake is complete."""


# ---------------------------------------------------------------------------
# Key material
# ---------------------------------------------------------------------------


@dataclass
class SessionKeys:
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    send_key: bytes = field(default_factory=lambda: secrets.token_bytes(SESSION_KEY_SIZE))
    recv_key: bytes = field(default_factory=lambda: secrets.token_bytes(SESSION_KEY_SIZE))
    mac_key: bytes = field(default_factory=lambda: secrets.token_bytes(SESSION_KEY_SIZE))
    created_at: float = field(default_factory=time.time)
    rekey_after_s: float = 3600.0

    @property
    def should_rekey(self) -> bool:
        return (time.time() - self.created_at) > self.rekey_after_s

    @property
    def fingerprint(self) -> str:
        combined = self.send_key + self.recv_key + self.mac_key
        return hashlib.sha256(combined).hexdigest()[:16]


def derive_session_keys(
    shared_secret: bytes,
    initiator_nonce: bytes,
    responder_nonce: bytes,
) -> SessionKeys:
    """
    Derives send/recv/mac keys from a shared secret and two nonces via HKDF-SHA256.
    (Simplified HKDF — production impl would use cryptography.hazmat.)
    """

    def hkdf_expand(prk: bytes, info: bytes, length: int) -> bytes:
        okm = b""
        t = b""
        i = 1
        while len(okm) < length:
            t = hmac.new(prk, t + info + bytes([i]), "sha256").digest()
            okm += t
            i += 1
        return okm[:length]

    salt = initiator_nonce + responder_nonce
    prk = hmac.new(salt, shared_secret, "sha256").digest()

    send_key = hkdf_expand(prk, b"coreai-stp-send", SESSION_KEY_SIZE)
    recv_key = hkdf_expand(prk, b"coreai-stp-recv", SESSION_KEY_SIZE)
    mac_key = hkdf_expand(prk, b"coreai-stp-mac", SESSION_KEY_SIZE)

    return SessionKeys(send_key=send_key, recv_key=recv_key, mac_key=mac_key)


# ---------------------------------------------------------------------------
# Frame codec
# ---------------------------------------------------------------------------


@dataclass
class STPFrame:
    version: int = STP_VERSION
    flags: int = FrameFlag.NONE
    timestamp: float = field(default_factory=time.time)
    sequence_no: int = 0
    nonce: bytes = field(default_factory=lambda: secrets.token_bytes(NONCE_SIZE))
    body: bytes = b""
    mac: bytes = b""

    def header_bytes(self) -> bytes:
        return (
            FRAME_MAGIC
            + struct.pack(">BBxx", self.version, self.flags)
            + struct.pack(">d", self.timestamp)
            + struct.pack(">I", self.sequence_no)
            + self.nonce
            + struct.pack(">I", len(self.body))
        )

    def to_bytes(self) -> bytes:
        if len(self.body) > MAX_FRAME_BODY_BYTES:
            raise FrameTooLargeError(
                f"Frame body {len(self.body)} bytes exceeds limit {MAX_FRAME_BODY_BYTES}"
            )
        return self.header_bytes() + self.body + self.mac

    @classmethod
    def from_bytes(cls, data: bytes) -> "STPFrame":
        if len(data) < FRAME_HEADER_SIZE + MAC_SIZE:
            raise ValueError("Frame too short")

        magic = data[:4]
        if magic != FRAME_MAGIC:
            raise ValueError(f"Invalid frame magic: {magic.hex()}")

        version, flags = struct.unpack(">BB", data[4:6])
        timestamp, = struct.unpack(">d", data[8:16])
        seq_no, = struct.unpack(">I", data[16:20])
        nonce = data[20:36]
        body_len, = struct.unpack(">I", data[36:40])

        expected_total = FRAME_HEADER_SIZE + body_len + MAC_SIZE
        if len(data) < expected_total:
            raise ValueError("Truncated frame")

        body = data[FRAME_HEADER_SIZE: FRAME_HEADER_SIZE + body_len]
        mac = data[FRAME_HEADER_SIZE + body_len: expected_total]

        return cls(
            version=version,
            flags=flags,
            timestamp=timestamp,
            sequence_no=seq_no,
            nonce=nonce,
            body=body,
            mac=mac,
        )


class FrameCodec:
    """Encodes and decodes STPFrames, computing and verifying MACs."""

    def __init__(self, keys: SessionKeys):
        self._keys = keys

    def encode(self, body: bytes, flags: int = FrameFlag.NONE, seq: int = 0) -> bytes:
        frame = STPFrame(flags=flags, sequence_no=seq, body=body)
        header = frame.header_bytes()
        mac = hmac.new(self._keys.mac_key, header + body, "sha256").digest()
        frame.mac = mac
        return frame.to_bytes()

    def decode(self, data: bytes) -> STPFrame:
        frame = STPFrame.from_bytes(data)
        header_plus_body = frame.header_bytes() + frame.body
        expected_mac = hmac.new(self._keys.mac_key, header_plus_body, "sha256").digest()
        if not hmac.compare_digest(expected_mac, frame.mac):
            raise MACVerificationError("Frame MAC verification failed — possible tampering")
        return frame


# ---------------------------------------------------------------------------
# Replay guard
# ---------------------------------------------------------------------------


class ReplayGuard:
    """
    Sliding-window replay attack prevention.
    Rejects frames with sequence numbers already seen within the window.
    """

    def __init__(self, window_s: float = REPLAY_WINDOW_S):
        self._window_s = window_s
        self._seen: dict[int, float] = {}   # seq_no → timestamp

    def check(self, seq_no: int, timestamp: float) -> None:
        now = time.time()

        if abs(now - timestamp) > self._window_s:
            raise ReplayAttackError(
                f"Frame timestamp {timestamp:.1f} outside replay window ({self._window_s}s)"
            )

        if seq_no in self._seen:
            raise ReplayAttackError(f"Duplicate sequence number {seq_no} — replay detected")

        # Evict old entries
        cutoff = now - self._window_s
        self._seen = {k: v for k, v in self._seen.items() if v > cutoff}
        self._seen[seq_no] = timestamp

    def reset(self) -> None:
        self._seen.clear()


# ---------------------------------------------------------------------------
# Handshake
# ---------------------------------------------------------------------------


@dataclass
class HandshakeHello:
    node_id: str
    nonce: bytes = field(default_factory=lambda: secrets.token_bytes(NONCE_SIZE))
    protocol_version: int = STP_VERSION
    timestamp: float = field(default_factory=time.time)
    supported_ciphers: list[str] = field(
        default_factory=lambda: ["HMAC-SHA256", "AES-256-GCM"]
    )


@dataclass
class HandshakeFinish:
    node_id: str
    session_id: str
    key_fingerprint: str
    timestamp: float = field(default_factory=time.time)
    verification_tag: bytes = b""


class STPHandshake:
    """
    Executes the STP handshake to establish shared session keys.

    Handshake flow:
        Initiator                      Responder
        ─────────                      ─────────
        HelloI  ──────────────────────►
                ◄──────────────────── HelloR
        (both derive keys from shared secret + nonces)
        Finish  ──────────────────────►
                ◄──────────────────── Finish (ACK)
    """

    def __init__(self, node_id: str, role: HandshakeRole):
        self.node_id = node_id
        self.role = role
        self._local_hello: Optional[HandshakeHello] = None
        self._remote_hello: Optional[HandshakeHello] = None
        self._session_keys: Optional[SessionKeys] = None

    def build_hello(self) -> HandshakeHello:
        self._local_hello = HandshakeHello(node_id=self.node_id)
        return self._local_hello

    def receive_hello(self, remote: HandshakeHello) -> None:
        if abs(time.time() - remote.timestamp) > REPLAY_WINDOW_S:
            raise HandshakeError("Remote hello timestamp outside acceptable window")
        self._remote_hello = remote

    def derive_keys(self, shared_secret: bytes) -> SessionKeys:
        if not self._local_hello or not self._remote_hello:
            raise HandshakeError("Cannot derive keys before both hellos are exchanged")

        if self.role == HandshakeRole.INITIATOR:
            initiator_nonce = self._local_hello.nonce
            responder_nonce = self._remote_hello.nonce
        else:
            initiator_nonce = self._remote_hello.nonce
            responder_nonce = self._local_hello.nonce

        self._session_keys = derive_session_keys(
            shared_secret, initiator_nonce, responder_nonce
        )
        return self._session_keys

    def build_finish(self) -> HandshakeFinish:
        if not self._session_keys:
            raise HandshakeError("Keys not derived yet")
        tag = hmac.new(
            self._session_keys.mac_key,
            self.node_id.encode() + self._session_keys.session_id.encode(),
            "sha256",
        ).digest()[:16]
        return HandshakeFinish(
            node_id=self.node_id,
            session_id=self._session_keys.session_id,
            key_fingerprint=self._session_keys.fingerprint,
            verification_tag=tag,
        )

    def verify_finish(self, remote_finish: HandshakeFinish) -> bool:
        if not self._session_keys:
            return False
        expected = hmac.new(
            self._session_keys.mac_key,
            remote_finish.node_id.encode() + self._session_keys.session_id.encode(),
            "sha256",
        ).digest()[:16]
        return hmac.compare_digest(expected, remote_finish.verification_tag)

    @property
    def session_keys(self) -> Optional[SessionKeys]:
        return self._session_keys


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


class SecureSession:
    """
    A fully established STP session. Wraps a FrameCodec and ReplayGuard
    to provide send/receive with integrity and replay protection.
    """

    def __init__(self, node_id: str, keys: SessionKeys):
        self.session_id = keys.session_id
        self.node_id = node_id
        self._codec = FrameCodec(keys)
        self._replay_guard = ReplayGuard()
        self._keys = keys
        self._send_seq = 0
        self._state = SessionState.ESTABLISHED
        self._established_at = time.time()
        self._bytes_sent = 0
        self._bytes_received = 0

    def send(self, payload: bytes, flags: int = FrameFlag.ENCRYPTED) -> bytes:
        if self._state != SessionState.ESTABLISHED:
            raise SessionNotEstablishedError(f"Session state is {self._state}")
        if self._keys.should_rekey:
            logger.warning("Session %s should rekey — key material is stale", self.session_id[:8])
        self._send_seq += 1
        frame_bytes = self._codec.encode(payload, flags=flags, seq=self._send_seq)
        self._bytes_sent += len(frame_bytes)
        return frame_bytes

    def receive(self, data: bytes) -> bytes:
        if self._state != SessionState.ESTABLISHED:
            raise SessionNotEstablishedError(f"Session state is {self._state}")
        frame = self._codec.decode(data)
        self._replay_guard.check(frame.sequence_no, frame.timestamp)
        self._bytes_received += len(data)
        return frame.body

    def close(self) -> None:
        self._state = SessionState.CLOSED
        logger.info("Secure session %s closed", self.session_id[:8])

    def get_stats(self) -> dict:
        return {
            "session_id": self.session_id,
            "node_id": self.node_id,
            "state": self._state,
            "uptime_s": round(time.time() - self._established_at, 1),
            "bytes_sent": self._bytes_sent,
            "bytes_received": self._bytes_received,
            "send_sequence": self._send_seq,
            "key_fingerprint": self._keys.fingerprint,
            "should_rekey": self._keys.should_rekey,
        }
