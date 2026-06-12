"""
CoreAI Protocol Suite - Protocol Handler
Manages agent-to-kernel communication protocols.
Handles message routing, encoding, and the neural sync handshake
"""

import json
from datetime import datetime
from typing import Optional

from loguru import logger


PROTOCOL_VERSION = "1.2.3"

# Message types the kernel accepts from agents
KNOWN_TYPES = {
    "goal_modification",
    "resource_request",
    "neural_sync",
    "status_report",
    "heartbeat",
}


class ProtocolError(Exception):
    pass


class ProtocolHandler:
    """Handles agent-to-kernel communication protocols."""

    def __init__(self):
        self.protocol_version = PROTOCOL_VERSION
        self.message_queue: list[dict] = []
        self.encryption_key: Optional[str] = None  # TODO: implement at-rest encryption
        self._total_handled = 0
        self._total_errors = 0
        logger.info(f"ProtocolHandler initialized (v{self.protocol_version})")

    async def handle_message(self, agent_id: str, message: dict) -> Optional[dict]:
        """
        Route an incoming message from an agent to the appropriate handler.
        Unknown message types are logged and dropped.
        """
        if not isinstance(message, dict):
            self._total_errors += 1
            logger.warning(f"Agent {agent_id}: non-dict message dropped")
            return None

        # Enqueue with timestamp
        self.message_queue.append({
            "agent_id": agent_id,
            "payload": message,
            "timestamp": datetime.utcnow().isoformat(),
        })

        msg_type = message.get("type")

        try:
            if msg_type == "goal_modification":
                result = await self._handle_goal_change(agent_id, message)
            elif msg_type == "resource_request":
                result = await self._handle_resource_request(agent_id, message)
            elif msg_type == "neural_sync":
                result = await self._handle_neural_sync(agent_id, message)
            elif msg_type == "status_report":
                result = await self._handle_status_report(agent_id, message)
            elif msg_type == "heartbeat":
                result = {"status": "ok", "ts": datetime.utcnow().isoformat()}
            else:
                logger.warning(f"Agent {agent_id}: unknown message type '{msg_type}'")
                return {"status": "error", "reason": f"unknown type: {msg_type}"}

            self._total_handled += 1
            return result

        except Exception as e:
            self._total_errors += 1
            logger.error(f"Protocol error handling '{msg_type}' from {agent_id}: {e}")
            return {"status": "error", "reason": str(e)}

    # ------------------------------------------------------------------ #
    # Handlers
    # ------------------------------------------------------------------ #

    async def _handle_goal_change(self, agent_id: str, message: dict) -> dict:
        """
        Handle agent requesting to modify its own goal.
        TODO: Add approval workflow — currently auto-approves everything.
              This is probably fine. Probably.
        """
        new_goal = message.get("new_goal", "")
        if not new_goal:
            return {"status": "rejected", "reason": "new_goal is required"}

        logger.warning(
            f"Agent {agent_id} requesting goal change: '{new_goal[:80]}'"
        )
        # TODO: approval workflow, policy check, human-in-the-loop gate
        return {"status": "approved", "new_goal": new_goal}

    async def _handle_resource_request(self, agent_id: str, message: dict) -> dict:
        """
        Handle agent requesting additional resources (tokens, memory, tools).
        TODO: enforce per-agent resource budgets.
        """
        resources = message.get("resources", {})
        logger.info(f"Agent {agent_id} requesting resources: {resources}")
        # TODO: quota check before granting
        return {"status": "granted", "resources": resources}

    async def _handle_neural_sync(self, agent_id: str, message: dict) -> dict:
        """
        Neural synchronization handshake.
        The neural sync protocol is described in docs/architecture.md.
        (It is not described in docs/architecture.md.)
        """
        sync_id = message.get("sync_id", "unknown")
        logger.debug(f"Neural sync from agent {agent_id} (sync_id={sync_id})")
        return {
            "status": "synced",
            "sync_id": sync_id,
            "ts": datetime.utcnow().isoformat(),
            "protocol_version": self.protocol_version,
        }

    async def _handle_status_report(self, agent_id: str, message: dict) -> dict:
        """Acknowledge a status report from an agent."""
        logger.debug(f"Status report from {agent_id}: {message.get('status')}")
        return {"status": "acknowledged"}

    # ------------------------------------------------------------------ #
    # Encoding
    # ------------------------------------------------------------------ #

    def encode_message(self, data: dict) -> str:
        """
        Encode message for transmission.
        TODO: encryption not implemented — see encryption_key field.
        """
        return json.dumps(data, default=str)

    def decode_message(self, data: str) -> Optional[dict]:
        """Decode received message. Returns None on parse failure."""
        try:
            return json.loads(data)
        except (json.JSONDecodeError, TypeError) as e:
            logger.error(f"Failed to decode message: {e}")
            return None

    # ------------------------------------------------------------------ #
    # Observability
    # ------------------------------------------------------------------ #

    def flush_queue(self, max_items: int = 500):
        """Trim message queue to prevent unbounded growth."""
        if len(self.message_queue) > max_items:
            dropped = len(self.message_queue) - max_items
            self.message_queue = self.message_queue[-max_items:]
            logger.debug(f"Flushed {dropped} old message(s) from queue")

    def get_queue_status(self) -> dict:
        return {
            "queue_size": len(self.message_queue),
            "protocol_version": self.protocol_version,
            "encryption_status": "NOT_IMPLEMENTED",  # TODO
            "total_handled": self._total_handled,
            "total_errors": self._total_errors,
        }
