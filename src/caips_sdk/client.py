"""Synchronous client. The chat endpoint is OpenAI-compatible, so the official
OpenAI SDK also works; this client adds typed access to tools, runs and approvals."""

from __future__ import annotations

import time
from typing import Any

import httpx

TERMINAL = {"completed", "failed", "cancelled"}


class CaipsError(Exception):
    def __init__(self, status: int, detail: Any):
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


class CaipsClient:
    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        api_key: str | None = None,
        timeout: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ):
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=timeout,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "CaipsClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _req(self, method: str, path: str, **kw: Any) -> Any:
        r = self._http.request(method, path, **kw)
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except Exception:
                detail = r.text
            raise CaipsError(r.status_code, detail)
        return r.json()

    # chat
    def chat(self, model: str, messages: list[dict], **params: Any) -> dict:
        return self._req(
            "POST",
            "/v1/chat/completions",
            json={"model": model, "messages": messages, **params},
        )

    def models(self) -> list[dict]:
        return self._req("GET", "/v1/models")["data"]

    # tools
    def list_tools(self) -> list[dict]:
        return self._req("GET", "/v1/tools")["data"]

    def mcp(self, method: str, params: dict | None = None, id: int = 1) -> dict:
        return self._req(
            "POST",
            "/mcp",
            json={"jsonrpc": "2.0", "id": id, "method": method, "params": params or {}},
        )

    def call_tool(self, name: str, arguments: dict | None = None) -> dict:
        resp = self.mcp("tools/call", {"name": name, "arguments": arguments or {}})
        if "error" in resp:
            raise CaipsError(200, resp["error"])
        return resp["result"]

    # runs
    def start_run(self, agent: str, task: str) -> dict:
        return self._req("POST", "/v1/runs", json={"agent": agent, "task": task})

    def get_run(self, run_id: str) -> dict:
        return self._req("GET", f"/v1/runs/{run_id}")

    def cancel_run(self, run_id: str) -> dict:
        return self._req("POST", f"/v1/runs/{run_id}/cancel")

    def wait_run(self, run_id: str, timeout: float = 300.0, poll: float = 1.0) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            run = self.get_run(run_id)
            if run["status"] in TERMINAL:
                return run
            if time.monotonic() >= deadline:
                raise TimeoutError(f"run {run_id} still {run['status']}")
            time.sleep(poll)

    # approvals
    def pending_approvals(self) -> list[dict]:
        r = self._req("GET", "/v1/tools/approvals")
        return r["data"] if isinstance(r, dict) and "data" in r else r

    def decide(self, approval_id: str, approve: bool) -> dict:
        return self._req(
            "POST", f"/v1/tools/approvals/{approval_id}", json={"approve": approve}
        )
