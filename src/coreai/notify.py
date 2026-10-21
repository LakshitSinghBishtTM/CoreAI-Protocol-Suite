"""Signed webhook notifications for operators (budget alerts, and later outages).

Same signing scheme as the approval webhook:
``X-CaiPS-Signature = HMAC-SHA256(secret, "<timestamp>.<body>")``, timestamp in
``X-CaiPS-Timestamp``. Delivery is fire-and-forget with a short timeout: a
failed notification is logged and never blocks a request. Payloads carry
subjects and numbers only, never prompts or keys.
"""

import asyncio
import hashlib
import hmac
import json
import time

import httpx
from loguru import logger


def sign(secret: str, timestamp: str, body: str) -> str:
    return hmac.new(
        secret.encode(), f"{timestamp}.{body}".encode(), hashlib.sha256
    ).hexdigest()


class WebhookNotifier:
    def __init__(self, url: str, secret: str, transport=None, timeout: float = 5.0):
        if not url.startswith(("http://", "https://")):
            raise ValueError("webhook url must be http(s)")
        if not secret:
            raise ValueError("webhook needs a signing secret")
        self.url, self.secret = url, secret
        self.transport, self.timeout = transport, timeout
        self.tasks: set = set()
        self.sent = self.failed = 0

    async def send(self, event: dict) -> bool:
        body = json.dumps(event, sort_keys=True, default=str)
        ts = str(int(time.time()))
        headers = {
            "Content-Type": "application/json",
            "X-CaiPS-Timestamp": ts,
            "X-CaiPS-Signature": sign(self.secret, ts, body),
        }
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout, transport=self.transport
            ) as http:
                (
                    await http.post(self.url, content=body, headers=headers)
                ).raise_for_status()
            self.sent += 1
            return True
        except Exception as exc:  # noqa: BLE001
            self.failed += 1
            logger.warning(f"notification webhook failed: {exc}")
            return False

    def __call__(self, event: dict) -> None:
        """Sync hook: schedule delivery if an event loop is running."""
        try:
            task = asyncio.get_running_loop().create_task(self.send(event))
        except RuntimeError:
            return
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
