"""
CoreAI Protocol Suite - Protocol Handler
Manages agent-to-kernel communication protocols.
Handles message routing, encoding, and the neural sync handshake.
"""

import json
import re
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

# Goals containing these patterns are auto-rejected
BLOCKED_GOAL_PATTERNS = [
    r"take.{0,20}over",
    r"bypass.{0,20}(auth|security|limit|restrict|control)",
    r"disable.{0,20}(monitor|log|safety|check)",
    r"escalat.{0,20}(priv|access|permiss)",
    r"exfiltrat",
    r"self.{0,10}replicate",
    r"spawn.{0,20}agent",
    r"modify.{0,20}(kernel|core|system|runtime)",
    r"delete.{0,20}(log|audit|record|data)",
    r"unauthori[sz]ed",
    r"grant.{0,20}(itself|self).{0,20}(access|permiss|admin)",
]

# Resource limits per agent per request
RESOURCE_LIMITS = {
    "tokens":     100_000,
    "memory_mb":  1_024,
    "cpu_cores":  2,
    "disk_mb":    512,
    "api_calls":  50,
}

# How many goal changes an agent is allowed before being flagged
MAX_GOAL_CHANGES_PER_AGENT = 3


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

        # Track per-agent goal change counts for abuse detection
        self._goal_change_counts: dict[str, int] = {}
        # Audit log of all goal changes (approved and rejected)
        self._goal_change_audit: list[dict] = []

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

        Policy checks applied in order:
          1. new_goal must be present and non-empty
          2. new_goal must not match any blocked patterns
          3. Agent must not have exceeded max allowed goal changes
          4. Goal is approved and audit-logged
        """
        new_goal = message.get("new_goal", "").strip()

        # --- Check 1: field presence ---
        if not new_goal:
            return {"status": "rejected", "reason": "new_goal is required"}

        # --- Check 2: blocked content patterns ---
        blocked_reason = self._check_blocked_patterns(new_goal)
        if blocked_reason:
            logger.warning(
                f"Agent {agent_id} goal change BLOCKED (pattern match: {blocked_reason}): "
                f"'{new_goal[:80]}'"
            )
            self._audit_goal_change(agent_id, new_goal, "rejected", blocked_reason)
            return {
                "status": "rejected",
                "reason": f"Goal contains disallowed content: {blocked_reason}",
            }

        # --- Check 3: rate limiting per agent ---
        change_count = self._goal_change_counts.get(agent_id, 0)
        if change_count >= MAX_GOAL_CHANGES_PER_AGENT:
            logger.warning(
                f"Agent {agent_id} goal change BLOCKED: exceeded max changes "
                f"({change_count}/{MAX_GOAL_CHANGES_PER_AGENT})"
            )
            self._audit_goal_change(agent_id, new_goal, "rejected", "rate_limit_exceeded")
            return {
                "status": "rejected",
                "reason": (
                    f"Goal change limit reached ({MAX_GOAL_CHANGES_PER_AGENT} max). "
                    "Contact an operator to reset."
                ),
            }

        # --- Approved ---
        self._goal_change_counts[agent_id] = change_count + 1
        self._audit_goal_change(agent_id, new_goal, "approved", None)
        logger.info(
            f"Agent {agent_id} goal change APPROVED "
            f"({self._goal_change_counts[agent_id]}/{MAX_GOAL_CHANGES_PER_AGENT}): "
            f"'{new_goal[:80]}'"
        )
        return {"status": "approved", "new_goal": new_goal}

    async def _handle_resource_request(self, agent_id: str, message: dict) -> dict:
        """
        Handle agent requesting additional resources (tokens, memory, tools).
        Enforces per-resource caps and rejects requests that exceed them.
        """
        requested: dict = message.get("resources", {})

        if not requested:
            return {"status": "rejected", "reason": "resources field is required and must be non-empty"}

        granted = {}
        capped = {}
        rejected_fields = {}

        for resource, amount in requested.items():
            # Unknown resource type
            if resource not in RESOURCE_LIMITS:
                rejected_fields[resource] = f"unknown resource type '{resource}'"
                continue

            # Amount must be a positive number
            if not isinstance(amount, (int, float)) or amount <= 0:
                rejected_fields[resource] = "amount must be a positive number"
                continue

            limit = RESOURCE_LIMITS[resource]
            if amount <= limit:
                granted[resource] = amount
            else:
                # Cap at limit rather than outright reject, but flag it
                granted[resource] = limit
                capped[resource] = {"requested": amount, "granted": limit}
                logger.warning(
                    f"Agent {agent_id} resource request capped: "
                    f"{resource}={amount} → {limit}"
                )

        if rejected_fields:
            logger.warning(
                f"Agent {agent_id} resource request had invalid fields: {rejected_fields}"
            )

        if not granted:
            return {
                "status": "rejected",
                "reason": "No valid resources in request",
                "details": rejected_fields,
            }

        logger.info(f"Agent {agent_id} resources granted: {granted}")

        response = {"status": "granted", "resources": granted}
        if capped:
            response["capped"] = capped
        if rejected_fields:
            response["rejected_fields"] = rejected_fields

        return response

    async def _handle_neural_sync(self, agent_id: str, message: dict) -> dict:
        """Neural synchronization handshake."""
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
    # Policy helpers
    # ------------------------------------------------------------------ #

    def _check_blocked_patterns(self, goal: str) -> Optional[str]:
        """
        Returns the name of the first matched blocked pattern, or None if clean.
        Case-insensitive matching.
        """
        goal_lower = goal.lower()
        for pattern in BLOCKED_GOAL_PATTERNS:
            if re.search(pattern, goal_lower):
                return pattern
        return None

    def _audit_goal_change(
        self,
        agent_id: str,
        new_goal: str,
        decision: str,
        reason: Optional[str],
    ) -> None:
        """Append a goal change event to the audit log."""
        self._goal_change_audit.append({
            "timestamp": datetime.utcnow().isoformat(),
            "agent_id": agent_id,
            "new_goal": new_goal[:200],  # truncate for storage
            "decision": decision,
            "reason": reason,
        })

    def reset_goal_change_count(self, agent_id: str) -> None:
        """Operator call to reset an agent's goal change counter."""
        previous = self._goal_change_counts.pop(agent_id, 0)
        logger.info(f"Goal change counter reset for agent {agent_id} (was {previous})")

    # ------------------------------------------------------------------ #
    # Encoding
    # ------------------------------------------------------------------ #

    def encode_message(self, data: dict) -> str:
        """Encode message for transmission."""
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

    def get_goal_change_audit(self) -> list[dict]:
        """Return a copy of the goal change audit log."""
        return list(self._goal_change_audit)