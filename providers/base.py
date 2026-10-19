from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncGenerator, Optional

from loguru import logger


@dataclass
class Message:
    role: str  # "user", "assistant", "system"
    content: str


def _count(value) -> int | None:
    """A real, non-negative token count, or None (mocks, missing, junk)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) if value >= 0 else None


@dataclass
class StreamUsage:
    """Usage for one streamed request.

    Providers call ``report()`` when the SDK gives token counts. The router
    fills in the rest when the stream ends: an estimate if nothing was
    reported (``estimated``), then ``cost_usd`` and the serving ``provider``.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    reported: bool = False  # the provider's SDK supplied the token counts
    estimated: bool = False  # the router estimated them from text length
    completed: bool = False  # the stream ended normally
    cost_usd: float = 0.0
    provider: str = ""

    def report(self, input_tokens, output_tokens, model=None) -> None:
        i, o = _count(input_tokens), _count(output_tokens)
        if i is None and o is None:
            return
        self.input_tokens = i if i is not None else self.input_tokens
        self.output_tokens = o if o is not None else self.output_tokens
        if isinstance(model, str) and model:
            self.model = model
        self.reported = True


@dataclass
class CompletionRequest:
    messages: list[Message]
    model: Optional[str] = None
    max_tokens: int = 1024
    temperature: float = 0.7
    stream: bool = False
    system_prompt: Optional[str] = None
    metadata: dict = field(default_factory=dict)
    # Set by the router for streams so providers can report token usage.
    stream_usage: Optional[StreamUsage] = field(default=None, repr=False, compare=False)


@dataclass
class CompletionResponse:
    content: str
    model: str
    provider: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: float
    cached: bool = False
    metadata: dict = field(default_factory=dict)


# Backstop timeout for the underlying HTTP client, in seconds. The router
# enforces the real per-provider timeouts (coreai.router) and cancels slow
# calls first; this only guarantees a hung connection is eventually closed.
# SDK-internal retries are disabled everywhere (max_retries=0) because the
# router owns retry and failover. Left on, they stack under the router's own
# retries and hide failures such as HTTP 529 from the circuit breaker.
SDK_TIMEOUT_S = 150.0


class BaseProvider(ABC):
    name: str = "base"
    default_model: str = ""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.total_requests = 0
        self.total_tokens = 0
        self.total_cost = 0.0
        logger.info(f"Initialized provider: {self.name}")

    @abstractmethod
    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        pass

    @abstractmethod
    async def stream(self, request: CompletionRequest) -> AsyncGenerator[str, None]:
        pass

    def estimate_cost(self, input_tokens: int, output_tokens: int, model: str) -> float:
        """Estimated USD for a call (routing, pre-flight checks). Does not count
        toward the unpriced-model metric; use ``_bill`` for a real response."""
        from coreai.pricing import get_pricing

        return get_pricing().cost(
            self.name, model, input_tokens, output_tokens, track=False
        )

    def _bill(self, input_tokens: int, output_tokens: int, model: str) -> float:
        """USD for a completed call. An unlisted model is counted and logged."""
        from coreai.pricing import get_pricing

        return get_pricing().cost(self.name, model, input_tokens, output_tokens)

    @abstractmethod
    def count_tokens(self, text: str, model: str) -> int:
        pass

    def _track(self, response: CompletionResponse):
        self.total_requests += 1
        self.total_tokens += response.input_tokens + response.output_tokens
        self.total_cost += response.cost_usd

    def stats(self) -> dict:
        return {
            "provider": self.name,
            "total_requests": self.total_requests,
            "total_tokens": self.total_tokens,
            "total_cost_usd": round(self.total_cost, 6),
        }
