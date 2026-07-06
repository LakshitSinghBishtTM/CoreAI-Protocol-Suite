"""
neural/model.py

CoreAI Neural Model Registry.
Defines model capability profiles, context window configs, and the
provider-to-model mapping used by the router for intelligent dispatch.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class ModelFamily(str, Enum):
    GPT = "gpt"
    CLAUDE = "claude"
    GEMINI = "gemini"
    GROK = "grok"
    DEEPSEEK = "deepseek"
    UNKNOWN = "unknown"


class ModelTier(str, Enum):
    FRONTIER = "frontier"
    BALANCED = "balanced"
    ECONOMY = "economy"
    REASONING = "reasoning"


class Modality(str, Enum):
    TEXT = "text"
    VISION = "vision"
    CODE = "code"
    AUDIO = "audio"
    EMBEDDING = "embedding"


@dataclass
class TokenPricing:
    input_per_million: float
    output_per_million: float
    cached_input_discount: float = 0.5

    def cost(
        self, input_tokens: int, output_tokens: int, cached: bool = False
    ) -> float:
        input_rate = self.input_per_million / 1_000_000
        if cached:
            input_rate *= self.cached_input_discount
        return round(
            input_tokens * input_rate
            + output_tokens * (self.output_per_million / 1_000_000),
            8,
        )


@dataclass
class ModelProfile:
    model_id: str
    provider: str
    family: ModelFamily
    tier: ModelTier
    context_window: int
    max_output_tokens: int
    modalities: list[Modality] = field(default_factory=lambda: [Modality.TEXT])
    pricing: Optional[TokenPricing] = None
    supports_streaming: bool = True
    supports_function_calling: bool = True
    supports_system_prompt: bool = True
    knowledge_cutoff: Optional[str] = None
    deprecated: bool = False
    notes: str = ""

    def estimate_cost(self, input_tokens: int, output_tokens: int) -> float:
        if self.pricing is None:
            return 0.0
        return self.pricing.cost(input_tokens, output_tokens)

    def fits_context(self, token_count: int) -> bool:
        return token_count <= self.context_window

    def to_dict(self) -> dict:
        return {
            "model_id": self.model_id,
            "provider": self.provider,
            "family": self.family,
            "tier": self.tier,
            "context_window": self.context_window,
            "max_output_tokens": self.max_output_tokens,
            "modalities": [m.value for m in self.modalities],
            "supports_streaming": self.supports_streaming,
            "supports_function_calling": self.supports_function_calling,
            "knowledge_cutoff": self.knowledge_cutoff,
            "deprecated": self.deprecated,
            "pricing": (
                {
                    "input_per_million_usd": self.pricing.input_per_million,
                    "output_per_million_usd": self.pricing.output_per_million,
                }
                if self.pricing
                else None
            ),
        }


MODEL_CATALOGUE: dict[str, ModelProfile] = {
    "gpt-4o": ModelProfile(
        model_id="gpt-4o",
        provider="openai",
        family=ModelFamily.GPT,
        tier=ModelTier.FRONTIER,
        context_window=128_000,
        max_output_tokens=16_384,
        modalities=[Modality.TEXT, Modality.VISION, Modality.CODE],
        pricing=TokenPricing(5.00, 15.00),
        knowledge_cutoff="2024-04",
    ),
    "gpt-4o-mini": ModelProfile(
        model_id="gpt-4o-mini",
        provider="openai",
        family=ModelFamily.GPT,
        tier=ModelTier.BALANCED,
        context_window=128_000,
        max_output_tokens=16_384,
        modalities=[Modality.TEXT, Modality.VISION, Modality.CODE],
        pricing=TokenPricing(0.15, 0.60),
        knowledge_cutoff="2024-07",
    ),
    "o3-mini": ModelProfile(
        model_id="o3-mini",
        provider="openai",
        family=ModelFamily.GPT,
        tier=ModelTier.REASONING,
        context_window=200_000,
        max_output_tokens=100_000,
        modalities=[Modality.TEXT, Modality.CODE],
        pricing=TokenPricing(1.10, 4.40),
        supports_streaming=False,
        knowledge_cutoff="2024-04",
    ),
    "claude-3-5-sonnet-20241022": ModelProfile(
        model_id="claude-3-5-sonnet-20241022",
        provider="anthropic",
        family=ModelFamily.CLAUDE,
        tier=ModelTier.FRONTIER,
        context_window=200_000,
        max_output_tokens=8_192,
        modalities=[Modality.TEXT, Modality.VISION, Modality.CODE],
        pricing=TokenPricing(3.00, 15.00),
        knowledge_cutoff="2024-04",
    ),
    "claude-3-5-haiku-20241022": ModelProfile(
        model_id="claude-3-5-haiku-20241022",
        provider="anthropic",
        family=ModelFamily.CLAUDE,
        tier=ModelTier.ECONOMY,
        context_window=200_000,
        max_output_tokens=8_192,
        modalities=[Modality.TEXT, Modality.CODE],
        pricing=TokenPricing(0.80, 4.00),
        knowledge_cutoff="2024-07",
    ),
    "gemini-1.5-pro": ModelProfile(
        model_id="gemini-1.5-pro",
        provider="gemini",
        family=ModelFamily.GEMINI,
        tier=ModelTier.FRONTIER,
        context_window=2_000_000,
        max_output_tokens=8_192,
        modalities=[Modality.TEXT, Modality.VISION, Modality.AUDIO, Modality.CODE],
        pricing=TokenPricing(1.25, 5.00),
        knowledge_cutoff="2024-05",
    ),
    "gemini-1.5-flash": ModelProfile(
        model_id="gemini-1.5-flash",
        provider="gemini",
        family=ModelFamily.GEMINI,
        tier=ModelTier.ECONOMY,
        context_window=1_000_000,
        max_output_tokens=8_192,
        modalities=[Modality.TEXT, Modality.VISION, Modality.CODE],
        pricing=TokenPricing(0.075, 0.30),
        knowledge_cutoff="2024-05",
    ),
    "grok-2": ModelProfile(
        model_id="grok-2",
        provider="grok",
        family=ModelFamily.GROK,
        tier=ModelTier.FRONTIER,
        context_window=131_072,
        max_output_tokens=4_096,
        modalities=[Modality.TEXT, Modality.CODE],
        pricing=TokenPricing(2.00, 10.00),
        knowledge_cutoff="2024-07",
    ),
    "grok-2-mini": ModelProfile(
        model_id="grok-2-mini",
        provider="grok",
        family=ModelFamily.GROK,
        tier=ModelTier.BALANCED,
        context_window=131_072,
        max_output_tokens=4_096,
        modalities=[Modality.TEXT, Modality.CODE],
        pricing=TokenPricing(0.20, 0.50),
        knowledge_cutoff="2024-07",
    ),
    "deepseek-chat": ModelProfile(
        model_id="deepseek-chat",
        provider="deepseek",
        family=ModelFamily.DEEPSEEK,
        tier=ModelTier.ECONOMY,
        context_window=65_536,
        max_output_tokens=8_192,
        modalities=[Modality.TEXT, Modality.CODE],
        pricing=TokenPricing(0.14, 0.28),
        knowledge_cutoff="2024-07",
    ),
    "deepseek-reasoner": ModelProfile(
        model_id="deepseek-reasoner",
        provider="deepseek",
        family=ModelFamily.DEEPSEEK,
        tier=ModelTier.REASONING,
        context_window=65_536,
        max_output_tokens=32_768,
        modalities=[Modality.TEXT, Modality.CODE],
        pricing=TokenPricing(0.55, 2.19),
        knowledge_cutoff="2024-07",
        notes="Chain-of-thought reasoning — slower but higher accuracy on complex tasks",
    ),
}

DEFAULT_MODELS: dict[str, str] = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-3-5-sonnet-20241022",
    "gemini": "gemini-1.5-flash",
    "grok": "grok-2-mini",
    "deepseek": "deepseek-chat",
}


class ModelRegistry:
    def __init__(self, catalogue: Optional[dict[str, ModelProfile]] = None):
        self._catalogue = catalogue or MODEL_CATALOGUE.copy()

    def get(self, model_id: str) -> Optional[ModelProfile]:
        return self._catalogue.get(model_id)

    def get_or_raise(self, model_id: str) -> ModelProfile:
        m = self.get(model_id)
        if m is None:
            raise KeyError(f"Unknown model: '{model_id}'")
        return m

    def default_for(self, provider: str) -> ModelProfile:
        model_id = DEFAULT_MODELS.get(provider)
        if not model_id:
            raise KeyError(f"No default model for provider '{provider}'")
        return self.get_or_raise(model_id)

    def list(
        self,
        provider: Optional[str] = None,
        tier: Optional[ModelTier] = None,
        modality: Optional[Modality] = None,
        min_context: Optional[int] = None,
        exclude_deprecated: bool = True,
    ) -> list[ModelProfile]:
        results = list(self._catalogue.values())
        if exclude_deprecated:
            results = [m for m in results if not m.deprecated]
        if provider:
            results = [m for m in results if m.provider == provider]
        if tier:
            results = [m for m in results if m.tier == tier]
        if modality:
            results = [m for m in results if modality in m.modalities]
        if min_context:
            results = [m for m in results if m.context_window >= min_context]
        return sorted(results, key=lambda m: (m.provider, m.tier))

    def cheapest(
        self, modality: Modality = Modality.TEXT, min_context: int = 0
    ) -> Optional[ModelProfile]:
        candidates = [
            m
            for m in self.list(modality=modality, min_context=min_context)
            if m.pricing
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda m: m.pricing.output_per_million)  # type: ignore

    def register(self, profile: ModelProfile) -> None:
        self._catalogue[profile.model_id] = profile

    def summary(self) -> dict:
        models = list(self._catalogue.values())
        return {
            "total_models": len(models),
            "providers": list({m.provider for m in models}),
            "by_tier": {
                t.value: sum(1 for m in models if m.tier == t) for t in ModelTier
            },
        }


_registry: Optional[ModelRegistry] = None


def get_registry() -> ModelRegistry:
    global _registry
    if _registry is None:
        _registry = ModelRegistry()
    return _registry
