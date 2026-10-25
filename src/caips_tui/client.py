"""Async HTTP client for the TUI. One method per kind of call; panes pass paths.

Errors never carry upstream secrets: the server already redacts provider error
text, and this client only forwards the ``detail`` field it was sent.
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator, Callable, Optional
from urllib.parse import quote

import httpx


class ApiError(Exception):
    """An HTTP error (status >= 400) or a network failure (status 0)."""

    def __init__(self, status: int, detail: Any):
        super().__init__(f"HTTP {status}: {detail}" if status else str(detail))
        self.status = status
        self.detail = detail


def q(value: Any) -> str:
    """Percent-encode an id for use inside a URL path."""
    return quote(str(value), safe="")


def _detail(r: httpx.Response) -> Any:
    try:
        body = r.json()
    except ValueError:
        return r.text or r.reason_phrase
    if isinstance(body, dict):
        # FastAPI: {"detail": ...}; OpenAI envelope: {"error": {"message": ...}}
        if "detail" in body:
            return body["detail"]
        err = body.get("error")
        if isinstance(err, dict) and "message" in err:
            return err["message"]
        return err or body
    return body


class Client:
    def __init__(
        self,
        base_url: str = "http://localhost:6969",
        api_key: Optional[str] = None,
        timeout: float = 60.0,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or ""
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            timeout=timeout,
            transport=transport,
        )

    @property
    def has_key(self) -> bool:
        return bool(self.api_key)

    def set_key(self, key: str) -> None:
        self.api_key = key
        self._http.headers["Authorization"] = f"Bearer {key}"

    def clear_key(self) -> None:
        self.api_key = ""
        self._http.headers.pop("Authorization", None)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def request(
        self,
        method: str,
        path: str,
        json: Any = None,
        params: Optional[dict] = None,
        timeout: Any = httpx.USE_CLIENT_DEFAULT,
        on_headers: Optional[Callable[[httpx.Headers], None]] = None,
    ) -> Any:
        """JSON (or text) body of a successful response; ``None`` when empty.

        ``timeout=None`` disables the timeout (model downloads can take minutes).
        """
        params = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        try:
            r = await self._http.request(
                method, path, json=json, params=params or None, timeout=timeout
            )
        except httpx.HTTPError as e:
            raise ApiError(
                0, f"cannot reach {self.base_url}: {e.__class__.__name__}: {e}"
            ) from e
        if r.status_code >= 400:
            raise ApiError(r.status_code, _detail(r))
        if on_headers:
            on_headers(r.headers)
        if not r.content:
            return None
        try:
            return r.json()
        except ValueError:
            return r.text

    async def stream(
        self,
        method: str,
        path: str,
        json: Any = None,
        on_headers: Optional[Callable[[httpx.Headers], None]] = None,
    ) -> AsyncIterator[tuple[str, str]]:
        """Server-sent events as ``(event, data)``; ``event`` is ``"message"`` by default.

        Raises ``ApiError`` if the server answers with an error status before
        the stream starts. ``[DONE]`` is yielded like any other data line.
        """
        try:
            async with self._http.stream(method, path, json=json) as r:
                if r.status_code >= 400:
                    await r.aread()
                    raise ApiError(r.status_code, _detail(r))
                if on_headers:
                    on_headers(r.headers)
                event = "message"
                data: list[str] = []
                async for line in r.aiter_lines():
                    if line == "":
                        if data:
                            yield event, "\n".join(data)
                        event, data = "message", []
                    elif line.startswith("event:"):
                        event = line[6:].strip()
                    elif line.startswith("data:"):
                        data.append(line[5:].lstrip(" "))
                if data:
                    yield event, "\n".join(data)
        except httpx.HTTPError as e:
            raise ApiError(0, f"connection lost: {e.__class__.__name__}: {e}") from e


def pretty(value: Any) -> str:
    """Human-readable rendering of a response body."""
    if isinstance(value, str):
        return value
    return json.dumps(value, indent=2, default=str, ensure_ascii=False)
