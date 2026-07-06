from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncGenerator, Optional
from loguru import logger


@dataclass
class Message:
    role: str  # "user", "assistant", "system"
    content: str


@dataclass
class CompletionRequest:
    messages: list[Message]
    model: Optional[str] = None
    max_tokens: int = 1024
    temperature: float = 0.7
    stream: bool = False
    system_prompt: Optional[str] = None
    metadata: dict = field(default_factory=dict)


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

    @abstractmethod
    def estimate_cost(self, input_tokens: int, output_tokens: int, model: str) -> float:
        pass

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
