"""Shared fakes for reliability tests. No network, no real providers."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from providers.base import CompletionRequest, CompletionResponse, Message


class StatusError(Exception):
    """Looks like an SDK APIStatusError: carries status_code and headers."""

    def __init__(self, status: int, message: str = "", headers: dict | None = None):
        super().__init__(message or f"HTTP {status}")
        self.status_code = status
        self.response = MagicMock()
        self.response.status_code = status
        self.response.headers = headers or {}


class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def response(name: str, cost: float = 0.001, latency_ms: float = 100.0):
    return CompletionResponse(
        content=f"response from {name}",
        model=f"{name}-model",
        provider=name,
        input_tokens=20,
        output_tokens=10,
        cost_usd=cost,
        latency_ms=latency_ms,
    )


def make_provider(name: str, cost: float = 0.001, behaviour=None):
    """A mock provider. *behaviour* is an exception, a callable, or a list of
    outcomes consumed in order (exceptions are raised, others returned)."""
    p = MagicMock()
    p.name = name
    p.default_model = f"{name}-model"
    p.estimate_cost = MagicMock(return_value=cost)
    p._bill = MagicMock(return_value=cost)

    if behaviour is None:
        p.complete = AsyncMock(return_value=response(name, cost))
    elif isinstance(behaviour, list):
        outcomes = list(behaviour)

        async def seq(request):
            outcome = outcomes.pop(0) if len(outcomes) > 1 else outcomes[0]
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome or response(name, cost)

        p.complete = AsyncMock(side_effect=seq)
    elif isinstance(behaviour, BaseException):
        p.complete = AsyncMock(side_effect=behaviour)
    else:
        p.complete = AsyncMock(side_effect=behaviour)
    return p


def hang_forever():
    async def hang(request):
        await asyncio.sleep(3600)

    return hang


def request(content: str = "hello", **kw) -> CompletionRequest:
    return CompletionRequest(
        messages=[Message(role="user", content=content)], max_tokens=256, **kw
    )


def make_router(providers, clock=None, **config):
    from coreai.router import Router, RoutingConfig, RoutingStrategy

    config.setdefault("enable_cache", False)
    config.setdefault("retry_initial_wait_seconds", 0.001)
    config.setdefault("retry_max_wait_seconds", 0.01)
    strategy = config.pop("strategy", RoutingStrategy.FALLBACK)
    cfg = RoutingConfig(strategy=strategy, **config)
    if clock is None:
        return Router(providers, cfg)
    return Router(providers, cfg, clock=clock)


def make_stream_provider(
    name,
    chunks=("a", "b", "c"),
    open_error=None,
    fail_after=None,
    hang_first=False,
    hang_after=None,
):
    """A mock provider whose stream() yields *chunks*.

    open_error:  raised before the first chunk (like an HTTP 529 on connect)
    fail_after:  (n, exc): raise exc after n chunks have been yielded
    hang_first:  never produce the first chunk
    hang_after:  hang after n chunks (idle stream)
    """
    p = make_provider(name)

    async def gen(req):
        if open_error is not None:
            raise open_error
        if hang_first:
            await asyncio.sleep(3600)
        for i, chunk in enumerate(chunks):
            if fail_after is not None and i == fail_after[0]:
                raise fail_after[1]
            if hang_after is not None and i == hang_after:
                await asyncio.sleep(3600)
            yield chunk
        if fail_after is not None and fail_after[0] >= len(chunks):
            raise fail_after[1]

    p.stream = MagicMock(side_effect=gen)
    return p


async def collect(agen):
    return [chunk async for chunk in agen]
