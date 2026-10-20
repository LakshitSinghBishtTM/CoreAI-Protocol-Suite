"""tools/webhook.py

Signed webhook for approval requests, so a human gets told when a destructive
tool is waiting. The body is signed like other CaiPS webhooks:
``X-CaiPS-Signature = HMAC-SHA256(secret, "<timestamp>.<body>")`` with the
timestamp in ``X-CaiPS-Timestamp``.

Argument values are NOT sent unless ``include_arguments`` is true: they can
hold secrets, and the approver can read them from the admin API.
"""

import asyncio
import hashlib
import hmac
import json
import time
from typing import Optional

import httpx
from loguru import logger

from .approvals import Approval


def sign(secret: str, timestamp: str, body: str) -> str:
    return hmac.new(secret.encode(), f"{timestamp}.{body}".encode(), hashlib.sha256).hexdigest()


def payload_for(a: Approval, include_arguments: bool) -> dict:
    p = {
        "event": "tool.approval_requested", "id": a.id, "caller": a.caller,
        "tool": a.tool, "argument_names": sorted(a.arguments),
        "review": "GET /v1/tools/approvals",
    }  # fmt: skip
    if include_arguments:
        p["arguments"] = a.arguments
    return p


class ApprovalWebhook:
    def __init__(self, url: str, secret: str, include_arguments: bool = False, transport=None):
        if not secret:
            raise ValueError("approval webhook needs a signing secret")
        self.url, self.secret = url, secret
        self.include_arguments, self.transport = include_arguments, transport
        self.tasks: set = set()

    async def send(self, a: Approval) -> bool:
        body = json.dumps(payload_for(a, self.include_arguments), sort_keys=True)
        ts = str(int(time.time()))
        headers = {
            "Content-Type": "application/json",
            "X-CaiPS-Timestamp": ts,
            "X-CaiPS-Signature": sign(self.secret, ts, body),
        }
        try:
            async with httpx.AsyncClient(timeout=5, transport=self.transport) as http:
                r = await http.post(self.url, content=body, headers=headers)
                r.raise_for_status()
            return True
        except Exception as exc:  # a failed notification must never block approvals
            logger.warning(f"approval webhook failed: {exc}")
            return False

    def __call__(self, a: Approval) -> None:
        """``ApprovalStore.on_request`` hook: fire and forget."""
        try:
            task = asyncio.get_running_loop().create_task(self.send(a))
        except RuntimeError:
            return
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
