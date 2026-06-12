"""
neural/inference.py

CoreAI Neural Inference Pipeline.
Manages request preprocessing, prompt assembly, provider dispatch,
response postprocessing, and output normalisation across all providers.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator, Optional

from neural.model import ModelProfile, ModelRegistry, get_registry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

MAX_RETRIES = 3
BASE_BACKOFF_S = 0.4
TRUNCATION_STRATEGY = "tail"   # "tail" | "head" | "middle"
DEFAULT_TEMPERATURE = 0.7
DEFAULT_MAX_TOKENS = 1024


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class InferenceStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    CACHED = "cached"


class FinishReason(str, Enum):
    STOP = "stop"
    LENGTH = "length"
    CONTENT_FILTER = "content_filter"
    TOOL_CALL = "tool_call"
    ERROR = "error"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


@dataclass
class Message:
    role: str       # "system" | "user" | "assistant" | "tool"
    content: str
    name: Optional[str] = None
    tool_call_id: Optional[str] = None

    def to_dict(self) -> dict:
        d = {"role": self.role, "content": self.content}
        if self.name:
            d["name"] = self.name
        if self.tool_call_id:
            d["tool_call_id"] = self.tool_call_id
        return d


@dataclass
class InferenceRequest:
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    messages: list[Message] = field(default_factory=list)
    model_id: Optional[str] = None
    provider: Optional[str] = None
    max_tokens: int = DEFAULT_MAX_TOKENS
    temperature: float = DEFAULT_TEMPERATURE
    top_p: float = 1.0
    stop_sequences: list[str] = field(default_factory=list)
    stream: bool = False
    tools: list[dict] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    @property
    def system_prompt(self) -> Optional[str]:
        for m in self.messages:
            if m.role == "system":
                return m.content
        return None

    @property
    def last_user_message(self) -> Optional[str]:
        for m in reversed(self.messages):
            if m.role == "user":
                return m.content
        return None

    def estimated_input_tokens(self) -> int:
        # rough: 4 chars ≈ 1 token
        total_chars = sum(len(m.content) for m in self.messages)
        return total_chars // 4


@dataclass
class UsageStats:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class InferenceResponse:
    request_id: str
    content: str
    model_id: str
    provider: str
    finish_reason: FinishReason = FinishReason.STOP
    status: InferenceStatus = InferenceStatus.COMPLETED
    usage: UsageStats = field(default_factory=UsageStats)
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    cached: bool = False
    tool_calls: list[dict] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    error: Optional[str] = None

    @property
    def success(self) -> bool:
        return self.status in (InferenceStatus.COMPLETED, InferenceStatus.CACHED)

    def to_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "content": self.content,
            "model": self.model_id,
            "provider": self.provider,
            "finish_reason": self.finish_reason,
            "input_tokens": self.usage.input_tokens,
            "output_tokens": self.usage.output_tokens,
            "cost_usd": self.cost_usd,
            "latency_ms": self.latency_ms,
            "cached": self.cached,
        }


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class InferenceError(RuntimeError):
    pass


class ContextWindowExceededError(InferenceError):
    pass


class ProviderUnavailableError(InferenceError):
    pass


class ContentFilteredError(InferenceError):
    pass


# ---------------------------------------------------------------------------
# Prompt assembler
# ---------------------------------------------------------------------------


class PromptAssembler:
    """
    Normalises message lists into provider-specific formats and
    handles context-window truncation when input is too long.
    """

    def assemble(
        self,
        messages: list[Message],
        model: ModelProfile,
        max_input_tokens: Optional[int] = None,
    ) -> list[dict]:
        limit = max_input_tokens or int(model.context_window * 0.85)
        messages = self._truncate(messages, limit)
        return [m.to_dict() for m in messages]

    def _truncate(self, messages: list[Message], token_limit: int) -> list[Message]:
        # Estimate token usage; preserve system prompt + last user message
        total = sum(len(m.content) // 4 for m in messages)
        if total <= token_limit:
            return messages

        logger.warning(
            "Input ~%d tokens exceeds limit %d — truncating (%s strategy)",
            total, token_limit, TRUNCATION_STRATEGY,
        )

        system = [m for m in messages if m.role == "system"]
        non_system = [m for m in messages if m.role != "system"]

        if TRUNCATION_STRATEGY == "tail":
            # Drop oldest non-system messages first
            while non_system and sum(len(m.content) // 4 for m in system + non_system) > token_limit:
                non_system.pop(0)
        elif TRUNCATION_STRATEGY == "head":
            while non_system and sum(len(m.content) // 4 for m in system + non_system) > token_limit:
                non_system.pop()
        # "middle" strategy: keep first + last, drop middle
        elif TRUNCATION_STRATEGY == "middle":
            while len(non_system) > 2 and sum(len(m.content) // 4 for m in system + non_system) > token_limit:
                non_system.pop(len(non_system) // 2)

        return system + non_system


# ---------------------------------------------------------------------------
# Response normaliser
# ---------------------------------------------------------------------------


class ResponseNormaliser:
    """
    Normalises provider-specific raw response dicts into InferenceResponse objects.
    Each provider returns slightly different field names and structures.
    """

    def normalise(
        self,
        raw: dict,
        provider: str,
        request: InferenceRequest,
        model: ModelProfile,
        latency_ms: float,
    ) -> InferenceResponse:
        try:
            if provider == "openai":
                return self._from_openai(raw, request, model, latency_ms)
            elif provider == "anthropic":
                return self._from_anthropic(raw, request, model, latency_ms)
            elif provider == "gemini":
                return self._from_gemini(raw, request, model, latency_ms)
            elif provider in ("grok", "deepseek"):
                return self._from_openai_compat(raw, request, model, latency_ms, provider)
            else:
                raise InferenceError(f"Unknown provider for normalisation: {provider}")
        except (KeyError, IndexError) as exc:
            raise InferenceError(f"Failed to normalise {provider} response: {exc}") from exc

    def _from_openai(self, raw: dict, req: InferenceRequest, model: ModelProfile, ms: float) -> InferenceResponse:
        choice = raw["choices"][0]
        usage = raw.get("usage", {})
        input_t = usage.get("prompt_tokens", 0)
        output_t = usage.get("completion_tokens", 0)
        finish = FinishReason(choice.get("finish_reason", "stop"))
        tool_calls = choice.get("message", {}).get("tool_calls") or []
        return InferenceResponse(
            request_id=req.request_id,
            content=choice["message"]["content"] or "",
            model_id=raw.get("model", model.model_id),
            provider="openai",
            finish_reason=finish,
            usage=UsageStats(input_tokens=input_t, output_tokens=output_t),
            cost_usd=model.estimate_cost(input_t, output_t),
            latency_ms=ms,
            tool_calls=tool_calls,
        )

    def _from_openai_compat(self, raw: dict, req: InferenceRequest, model: ModelProfile, ms: float, provider: str) -> InferenceResponse:
        r = self._from_openai(raw, req, model, ms)
        r.provider = provider
        return r

    def _from_anthropic(self, raw: dict, req: InferenceRequest, model: ModelProfile, ms: float) -> InferenceResponse:
        content_blocks = raw.get("content", [])
        text = " ".join(b.get("text", "") for b in content_blocks if b.get("type") == "text")
        usage = raw.get("usage", {})
        input_t = usage.get("input_tokens", 0)
        output_t = usage.get("output_tokens", 0)
        stop_reason = raw.get("stop_reason", "end_turn")
        finish = FinishReason.STOP if stop_reason == "end_turn" else FinishReason(stop_reason.replace("_", "") if stop_reason else "stop")
        return InferenceResponse(
            request_id=req.request_id,
            content=text,
            model_id=raw.get("model", model.model_id),
            provider="anthropic",
            finish_reason=finish,
            usage=UsageStats(input_tokens=input_t, output_tokens=output_t),
            cost_usd=model.estimate_cost(input_t, output_t),
            latency_ms=ms,
        )

    def _from_gemini(self, raw: dict, req: InferenceRequest, model: ModelProfile, ms: float) -> InferenceResponse:
        candidates = raw.get("candidates", [{}])
        parts = candidates[0].get("content", {}).get("parts", [{}])
        text = " ".join(p.get("text", "") for p in parts)
        usage = raw.get("usageMetadata", {})
        input_t = usage.get("promptTokenCount", 0)
        output_t = usage.get("candidatesTokenCount", 0)
        return InferenceResponse(
            request_id=req.request_id,
            content=text,
            model_id=model.model_id,
            provider="gemini",
            usage=UsageStats(input_tokens=input_t, output_tokens=output_t),
            cost_usd=model.estimate_cost(input_t, output_t),
            latency_ms=ms,
        )


# ---------------------------------------------------------------------------
# Inference pipeline
# ---------------------------------------------------------------------------


class InferencePipeline:
    """
    End-to-end inference pipeline:
      1. Assemble prompt (truncate if needed)
      2. Dispatch to provider client
      3. Normalise response
      4. Record usage stats
    """

    def __init__(
        self,
        registry: Optional[ModelRegistry] = None,
    ):
        self._registry = registry or get_registry()
        self._assembler = PromptAssembler()
        self._normaliser = ResponseNormaliser()
        self._stats = {
            "total_requests": 0,
            "total_errors": 0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_cost_usd": 0.0,
        }

    async def run(
        self,
        request: InferenceRequest,
        provider_client: Any,
    ) -> InferenceResponse:
        self._stats["total_requests"] += 1

        model_id = request.model_id
        provider = request.provider

        if not model_id and provider:
            model = self._registry.default_for(provider)
            model_id = model.model_id
        elif model_id:
            model = self._registry.get_or_raise(model_id)
            provider = provider or model.provider
        else:
            raise InferenceError("Either model_id or provider must be specified")

        assembled = self._assembler.assemble(request.messages, model)

        t0 = time.perf_counter()
        for attempt in range(MAX_RETRIES):
            try:
                raw = await provider_client.complete(
                    messages=assembled,
                    model=model_id,
                    max_tokens=request.max_tokens,
                    temperature=request.temperature,
                    stream=False,
                )
                latency_ms = (time.perf_counter() - t0) * 1000
                response = self._normaliser.normalise(raw, provider, request, model, latency_ms)
                self._record(response)
                return response

            except Exception as exc:  # pylint: disable=broad-except
                if attempt == MAX_RETRIES - 1:
                    self._stats["total_errors"] += 1
                    raise InferenceError(f"Inference failed after {MAX_RETRIES} attempts: {exc}") from exc
                backoff = BASE_BACKOFF_S * (2 ** attempt)
                logger.warning("Inference attempt %d failed: %s — retrying in %.1fs", attempt + 1, exc, backoff)
                await asyncio.sleep(backoff)

        raise InferenceError("Unreachable")  # satisfies type checker

    async def stream(
        self,
        request: InferenceRequest,
        provider_client: Any,
    ) -> AsyncIterator[str]:
        model_id = request.model_id or ""
        provider = request.provider or ""
        if model_id:
            model = self._registry.get_or_raise(model_id)
            provider = provider or model.provider
        else:
            model = self._registry.default_for(provider)
            model_id = model.model_id

        if not model.supports_streaming:
            raise InferenceError(f"Model {model_id} does not support streaming")

        assembled = self._assembler.assemble(request.messages, model)
        async for chunk in provider_client.stream(
            messages=assembled,
            model=model_id,
            max_tokens=request.max_tokens,
            temperature=request.temperature,
        ):
            yield chunk

    def _record(self, response: InferenceResponse) -> None:
        self._stats["total_input_tokens"] += response.usage.input_tokens
        self._stats["total_output_tokens"] += response.usage.output_tokens
        self._stats["total_cost_usd"] += response.cost_usd

    def get_stats(self) -> dict:
        return dict(self._stats)


_pipeline: Optional[InferencePipeline] = None


def get_pipeline() -> InferencePipeline:
    global _pipeline
    if _pipeline is None:
        _pipeline = InferencePipeline()
    return _pipeline