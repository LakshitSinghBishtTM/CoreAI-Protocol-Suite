"""Several API keys behind one provider name.

Requests rotate across the keys. A key that gets an auth, quota or rate-limit
error is parked for a cooldown and the request moves to the next key, so one
exhausted key does not fail the request while another still works. Anything
else (bad request, 5xx, timeout) is raised unchanged for the router's own
retry, failover and circuit-breaker logic.
"""

from __future__ import annotations

import time
from typing import AsyncGenerator, Callable

from loguru import logger

from .base import BaseProvider, CompletionRequest, CompletionResponse


def _key_errors() -> set:
    from coreai.errors import ErrorKind  # lazy: avoids import cycle

    return {ErrorKind.AUTH, ErrorKind.QUOTA, ErrorKind.RATE_LIMIT}


def classify(exc):
    from coreai.errors import classify as _classify  # lazy: avoids import cycle

    return _classify(exc)


class KeyPoolProvider(BaseProvider):
    def __init__(
        self,
        name: str,
        members: list[BaseProvider],
        labels: list[str],
        cooldown_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        if not members or len(members) != len(labels):
            raise ValueError("a key pool needs one label per member")
        first = members[0]
        self.name = name
        self.members = members
        self.labels = labels
        self.cooldown_s = cooldown_s
        self._clock = clock
        self._parked: dict[int, float] = {}
        self._next = 0
        self.default_model = first.default_model
        for attr in (
            "supports_tools",
            "supports_vision",
            "supports_embeddings",
            "default_embedding_model",
            "base_url",
        ):
            if hasattr(first, attr):
                setattr(self, attr, getattr(first, attr))

    # delegate anything else (fine_tuning_client, ...) to the first key
    def __getattr__(self, item: str):
        if item in ("members", "labels"):
            raise AttributeError(item)
        return getattr(self.members[0], item)

    @property
    def api_key(self) -> str:  # type: ignore[override]  # aggregate of the members
        return self.members[0].api_key

    @property
    def total_requests(self) -> int:  # type: ignore[override]  # aggregate of the members
        return sum(m.total_requests for m in self.members)

    @property
    def total_tokens(self) -> int:  # type: ignore[override]  # aggregate of the members
        return sum(m.total_tokens for m in self.members)

    @property
    def total_cost(self) -> float:  # type: ignore[override]  # aggregate of the members
        return sum(m.total_cost for m in self.members)

    def key_states(self) -> list[dict]:
        now = self._clock()
        return [
            {"label": lb, "parked": self._parked.get(i, 0) > now}
            for i, lb in enumerate(self.labels)
        ]

    def _order(self) -> list[int]:
        n = len(self.members)
        start, self._next = self._next % n, (self._next + 1) % n
        order = [(start + i) % n for i in range(n)]
        now = self._clock()
        live = [i for i in order if self._parked.get(i, 0) <= now]
        return live or order  # all parked: try them anyway rather than fail blind

    def _park(self, i: int, exc: BaseException) -> None:
        self._parked[i] = self._clock() + self.cooldown_s
        logger.warning(
            f"[{self.name}] key '{self.labels[i]}' parked for "
            f"{self.cooldown_s:.0f}s: {classify(exc).kind.value}"
        )

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        last: BaseException | None = None
        for i in self._order():
            try:
                return await self.members[i].complete(request)
            except Exception as e:
                if classify(e).kind not in _key_errors():
                    raise
                self._park(i, e)
                last = e
        assert last is not None
        raise last

    async def stream(self, request: CompletionRequest) -> AsyncGenerator[str, None]:
        last: BaseException | None = None
        for i in self._order():
            gen = self.members[i].stream(request)
            started = False
            try:
                async for chunk in gen:
                    started = True
                    yield chunk
                return
            except Exception as e:
                if started or classify(e).kind not in _key_errors():
                    raise
                self._park(i, e)
                last = e
        assert last is not None
        raise last

    async def embed(self, texts, model=None):
        last: BaseException | None = None
        for i in self._order():
            try:
                return await self.members[i].embed(texts, model)
            except Exception as e:
                if classify(e).kind not in _key_errors():
                    raise
                self._park(i, e)
                last = e
        assert last is not None
        raise last

    def count_tokens(self, text: str, model: str) -> int:
        return self.members[0].count_tokens(text, model)

    def estimate_cost(self, input_tokens: int, output_tokens: int, model: str) -> float:
        return self.members[0].estimate_cost(input_tokens, output_tokens, model)
