"""tools/approvals.py

Human approval for destructive tool calls. A pending request is bound to the
exact (caller, tool, arguments); approving it lets that one call run once.
In-memory, bounded and expiring, so it cannot grow without limit.
"""

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

PENDING, APPROVED, DENIED, USED = "pending", "approved", "denied", "used"


@dataclass
class Approval:
    id: str
    caller: str
    tool: str
    arguments: dict
    created: float
    status: str = PENDING
    decided_by: Optional[str] = None

    def public(self) -> dict:
        return {
            "id": self.id, "caller": self.caller, "tool": self.tool,
            "arguments": self.arguments, "status": self.status,
            "decided_by": self.decided_by,
        }  # fmt: skip


def _fingerprint(caller: str, tool: str, args: dict) -> str:
    return f"{caller}|{tool}|{json.dumps(args, sort_keys=True, default=str)}"


@dataclass
class ApprovalStore:
    ttl_s: float = 900.0
    max_items: int = 1000
    clock: Callable[[], float] = time.monotonic
    # Called with the new Approval, e.g. to fire a webhook. Must not raise.
    on_request: Optional[Callable[[Approval], None]] = None
    _items: dict = field(default_factory=dict)

    def _sweep(self) -> None:
        now = self.clock()
        for k in [k for k, a in self._items.items() if now - a.created > self.ttl_s]:
            del self._items[k]

    def request(self, caller: str, tool: str, arguments: dict) -> Approval:
        """Open (or reuse) a pending approval for exactly this call."""
        self._sweep()
        fp = _fingerprint(caller, tool, arguments)
        for a in self._items.values():
            if a.status in (PENDING, APPROVED) and _fingerprint(a.caller, a.tool, a.arguments) == fp:
                return a
        if len(self._items) >= self.max_items:
            raise RuntimeError("too many pending approvals")
        a = Approval(uuid.uuid4().hex[:16], caller, tool, dict(arguments), self.clock())
        self._items[a.id] = a
        if self.on_request:
            try:
                self.on_request(a)
            except Exception:
                pass
        return a

    def get(self, approval_id: str) -> Optional[Approval]:
        self._sweep()
        return self._items.get(approval_id)

    def pending(self) -> list[Approval]:
        self._sweep()
        return [a for a in self._items.values() if a.status == PENDING]

    def decide(self, approval_id: str, approve: bool, by: str) -> Approval:
        a = self.get(approval_id)
        if a is None:
            raise KeyError(approval_id)
        if a.status != PENDING:
            raise ValueError(f"approval already {a.status}")
        a.status = APPROVED if approve else DENIED
        a.decided_by = by
        return a

    def consume(self, caller: str, tool: str, arguments: dict) -> bool:
        """True once if this exact call was approved; the approval is spent."""
        self._sweep()
        fp = _fingerprint(caller, tool, arguments)
        for a in self._items.values():
            if a.status == APPROVED and _fingerprint(a.caller, a.tool, a.arguments) == fp:
                a.status = USED
                return True
        return False
