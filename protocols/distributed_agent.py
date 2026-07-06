"""
protocols/distributed_agent.py

CoreAI Distributed Agent Protocol.
Defines the wire protocol, message envelope, and coordination primitives
used by autonomous agents communicating across nodes in a CoreAI cluster.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Protocol constants
# ---------------------------------------------------------------------------

PROTOCOL_VERSION = "dap/1.2"
MAX_MESSAGE_SIZE_BYTES = 1 * 1024 * 1024  # 1 MB
DEFAULT_ACK_TIMEOUT_S = 5.0
MAX_ROUTING_HOPS = 8
AGENT_CHANNEL_PREFIX = "coreai.agent."


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class MessageType(str, Enum):
    TASK_ASSIGN = "task.assign"
    TASK_RESULT = "task.result"
    TASK_CANCEL = "task.cancel"
    HEARTBEAT = "heartbeat"
    CAPABILITY_ANNOUNCE = "capability.announce"
    CAPABILITY_QUERY = "capability.query"
    STATE_SYNC = "state.sync"
    ERROR = "error"
    ACK = "ack"


class DeliveryMode(str, Enum):
    AT_MOST_ONCE = "at_most_once"  # fire and forget
    AT_LEAST_ONCE = "at_least_once"  # retried until ack
    EXACTLY_ONCE = "exactly_once"  # deduped + ack


class AgentCapability(str, Enum):
    TEXT_COMPLETION = "text_completion"
    CODE_EXECUTION = "code_execution"
    WEB_SEARCH = "web_search"
    DATA_ANALYSIS = "data_analysis"
    MEMORY_READ = "memory_read"
    MEMORY_WRITE = "memory_write"
    TOOL_USE = "tool_use"


# ---------------------------------------------------------------------------
# Message envelope
# ---------------------------------------------------------------------------


@dataclass
class MessageEnvelope:
    """
    Wire-level message container for the Distributed Agent Protocol.
    All inter-agent communication is wrapped in this envelope.
    """

    msg_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    protocol_version: str = PROTOCOL_VERSION
    msg_type: MessageType = MessageType.TASK_ASSIGN
    sender_id: str = ""
    recipient_id: str = ""  # empty = broadcast
    correlation_id: Optional[str] = None  # links replies to requests
    delivery_mode: DeliveryMode = DeliveryMode.AT_LEAST_ONCE
    created_at: float = field(default_factory=time.time)
    ttl_s: float = 60.0
    hop_count: int = 0
    payload: dict = field(default_factory=dict)
    checksum: str = ""

    def __post_init__(self):
        if not self.checksum:
            self.checksum = self._compute_checksum()

    def _compute_checksum(self) -> str:
        raw = json.dumps(self.payload, sort_keys=True).encode()
        return hashlib.sha256(raw).hexdigest()[:16]

    @property
    def is_expired(self) -> bool:
        return (time.time() - self.created_at) > self.ttl_s

    @property
    def is_broadcast(self) -> bool:
        return not self.recipient_id

    def to_dict(self) -> dict:
        d = asdict(self)
        d["msg_type"] = self.msg_type.value
        d["delivery_mode"] = self.delivery_mode.value
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "MessageEnvelope":
        data["msg_type"] = MessageType(data["msg_type"])
        data["delivery_mode"] = DeliveryMode(data["delivery_mode"])
        return cls(**data)

    def make_reply(self, msg_type: MessageType, payload: dict) -> "MessageEnvelope":
        return MessageEnvelope(
            msg_type=msg_type,
            sender_id=self.recipient_id,
            recipient_id=self.sender_id,
            correlation_id=self.msg_id,
            delivery_mode=self.delivery_mode,
            payload=payload,
        )


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ProtocolViolationError(RuntimeError):
    """Raised when a message violates DAP protocol rules."""


class AckTimeoutError(asyncio.TimeoutError):
    """Raised when an AT_LEAST_ONCE message is not acknowledged in time."""


class RoutingLoopError(RuntimeError):
    """Raised when hop count exceeds MAX_ROUTING_HOPS."""


# ---------------------------------------------------------------------------
# Message router
# ---------------------------------------------------------------------------


class AgentMessageRouter:
    """
    In-process message router for DAP messages.
    In a real deployment this would sit on top of a message broker
    (NATS, RabbitMQ, Redis Streams). Here it uses asyncio queues.
    """

    def __init__(self):
        self._queues: dict[str, asyncio.Queue] = {}
        self._broadcast_handlers: list[Callable] = []
        self._pending_acks: dict[str, asyncio.Future] = {}
        self._delivered: set[str] = set()  # for exactly-once dedup

    def register_agent(self, agent_id: str) -> asyncio.Queue:
        if agent_id not in self._queues:
            self._queues[agent_id] = asyncio.Queue(maxsize=512)
            logger.debug("Router: registered agent %s", agent_id)
        return self._queues[agent_id]

    def deregister_agent(self, agent_id: str) -> None:
        self._queues.pop(agent_id, None)

    async def send(self, envelope: MessageEnvelope) -> None:
        if envelope.is_expired:
            logger.warning("Dropping expired message %s", envelope.msg_id[:8])
            return

        if envelope.hop_count >= MAX_ROUTING_HOPS:
            raise RoutingLoopError(
                f"Message {envelope.msg_id[:8]} exceeded max hops ({MAX_ROUTING_HOPS})"
            )

        envelope.hop_count += 1

        # Exactly-once dedup
        if envelope.delivery_mode == DeliveryMode.EXACTLY_ONCE:
            if envelope.msg_id in self._delivered:
                logger.debug("Dropping duplicate message %s", envelope.msg_id[:8])
                return
            self._delivered.add(envelope.msg_id)

        if envelope.is_broadcast:
            await self._broadcast(envelope)
        else:
            await self._unicast(envelope)

    async def _unicast(self, envelope: MessageEnvelope) -> None:
        queue = self._queues.get(envelope.recipient_id)
        if queue is None:
            logger.warning(
                "No queue for recipient %s — dropping message %s",
                envelope.recipient_id,
                envelope.msg_id[:8],
            )
            return
        await queue.put(envelope)

        if envelope.delivery_mode == DeliveryMode.AT_LEAST_ONCE:
            await self._await_ack(envelope)

    async def _broadcast(self, envelope: MessageEnvelope) -> None:
        for agent_id, queue in self._queues.items():
            if agent_id != envelope.sender_id:
                try:
                    queue.put_nowait(envelope)
                except asyncio.QueueFull:
                    logger.warning(
                        "Queue full for agent %s — broadcast dropped", agent_id
                    )

    async def _await_ack(self, envelope: MessageEnvelope) -> None:
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending_acks[envelope.msg_id] = fut
        try:
            await asyncio.wait_for(asyncio.shield(fut), timeout=DEFAULT_ACK_TIMEOUT_S)
        except asyncio.TimeoutError:
            self._pending_acks.pop(envelope.msg_id, None)
            raise AckTimeoutError(
                f"No ACK received for message {envelope.msg_id[:8]} "
                f"from {envelope.recipient_id}"
            )

    def acknowledge(self, msg_id: str) -> None:
        fut = self._pending_acks.pop(msg_id, None)
        if fut and not fut.done():
            fut.set_result(True)


# ---------------------------------------------------------------------------
# Agent capability registry
# ---------------------------------------------------------------------------


@dataclass
class AgentCapabilityRecord:
    agent_id: str
    capabilities: list[AgentCapability]
    model: str = ""
    max_concurrent_tasks: int = 4
    announced_at: float = field(default_factory=time.time)
    region: str = "us-east-1"


class CapabilityRegistry:
    """
    Tracks which agents can handle which task types.
    Used by the orchestrator to route tasks to capable agents.
    """

    def __init__(self):
        self._records: dict[str, AgentCapabilityRecord] = {}

    def announce(self, record: AgentCapabilityRecord) -> None:
        self._records[record.agent_id] = record
        logger.debug(
            "Capability announced: %s → %s",
            record.agent_id,
            [c.value for c in record.capabilities],
        )

    def find(
        self,
        capability: AgentCapability,
        region: Optional[str] = None,
    ) -> list[AgentCapabilityRecord]:
        results = [r for r in self._records.values() if capability in r.capabilities]
        if region:
            results = [r for r in results if r.region == region]
        return sorted(results, key=lambda r: r.max_concurrent_tasks, reverse=True)

    def remove(self, agent_id: str) -> None:
        self._records.pop(agent_id, None)

    def all_agents(self) -> list[str]:
        return list(self._records.keys())


# ---------------------------------------------------------------------------
# Protocol handler
# ---------------------------------------------------------------------------


class DistributedAgentProtocol:
    """
    High-level DAP protocol handler.
    Wraps the router + capability registry and exposes a clean API
    for agents to send/receive typed messages.
    """

    def __init__(self):
        self.router = AgentMessageRouter()
        self.capabilities = CapabilityRegistry()

    def register_agent(
        self,
        agent_id: str,
        capabilities: list[AgentCapability],
        model: str = "",
        region: str = "us-east-1",
    ) -> asyncio.Queue:
        record = AgentCapabilityRecord(
            agent_id=agent_id,
            capabilities=capabilities,
            model=model,
            region=region,
        )
        self.capabilities.announce(record)
        return self.router.register_agent(agent_id)

    def deregister_agent(self, agent_id: str) -> None:
        self.capabilities.remove(agent_id)
        self.router.deregister_agent(agent_id)

    async def send_task(
        self,
        sender_id: str,
        recipient_id: str,
        task_payload: dict,
        delivery: DeliveryMode = DeliveryMode.AT_LEAST_ONCE,
    ) -> str:
        envelope = MessageEnvelope(
            msg_type=MessageType.TASK_ASSIGN,
            sender_id=sender_id,
            recipient_id=recipient_id,
            delivery_mode=delivery,
            payload=task_payload,
        )
        await self.router.send(envelope)
        return envelope.msg_id

    async def send_result(
        self,
        original: MessageEnvelope,
        result: Any,
        error: Optional[str] = None,
    ) -> None:
        payload = {"result": result, "error": error}
        reply = original.make_reply(MessageType.TASK_RESULT, payload)
        await self.router.send(reply)
        self.router.acknowledge(original.msg_id)

    async def broadcast_heartbeat(self, agent_id: str, status: dict) -> None:
        envelope = MessageEnvelope(
            msg_type=MessageType.HEARTBEAT,
            sender_id=agent_id,
            recipient_id="",
            delivery_mode=DeliveryMode.AT_MOST_ONCE,
            ttl_s=10.0,
            payload={"agent_id": agent_id, "status": status, "ts": time.time()},
        )
        await self.router.send(envelope)

    async def receive(
        self, agent_id: str, timeout_s: Optional[float] = None
    ) -> Optional[MessageEnvelope]:
        queue = self._queues_for(agent_id)
        if queue is None:
            return None
        try:
            if timeout_s:
                return await asyncio.wait_for(queue.get(), timeout=timeout_s)
            return queue.get_nowait()
        except (asyncio.TimeoutError, asyncio.QueueEmpty):
            return None

    def _queues_for(self, agent_id: str) -> Optional[asyncio.Queue]:
        return self.router._queues.get(agent_id)

    def get_stats(self) -> dict:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "registered_agents": len(self.router._queues),
            "pending_acks": len(self.router._pending_acks),
            "deduplicated_messages": len(self.router._delivered),
            "capability_registry_size": len(self.capabilities._records),
        }


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_protocol: Optional[DistributedAgentProtocol] = None


def get_agent_protocol() -> DistributedAgentProtocol:
    global _protocol
    if _protocol is None:
        _protocol = DistributedAgentProtocol()
    return _protocol
