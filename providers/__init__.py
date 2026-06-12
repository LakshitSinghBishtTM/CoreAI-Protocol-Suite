import os

from .base import BaseProvider, CompletionRequest, CompletionResponse, Message
from .openai import OpenAIProvider
from .anthropic import AnthropicProvider
from .gemini import GeminiProvider
from .grok import GrokProvider
from .deepseek import DeepSeekProvider

__all__ = [
    "BaseProvider",
    "CompletionRequest",
    "CompletionResponse",
    "Message",
    "OpenAIProvider",
    "AnthropicProvider",
    "GeminiProvider",
    "GrokProvider",
    "DeepSeekProvider",
    "load_providers",
]

PROVIDER_MAP = {
    "openai": (OpenAIProvider, "OPENAI_API_KEY"),
    "anthropic": (AnthropicProvider, "ANTHROPIC_API_KEY"),
    "gemini": (GeminiProvider, "GEMINI_API_KEY"),
    "grok": (GrokProvider, "GROK_API_KEY"),
    "deepseek": (DeepSeekProvider, "DEEPSEEK_API_KEY"),
}


def load_providers(enabled: list[str] | None = None) -> dict[str, BaseProvider]:
    """
    Load and return all providers that have API keys set in env.
    Optionally filter by enabled list.
    """
    providers = {}
    targets = enabled or list(PROVIDER_MAP.keys())

    for name in targets:
        if name not in PROVIDER_MAP:
            continue
        cls, env_key = PROVIDER_MAP[name]
        api_key = os.getenv(env_key)
        if not api_key:
            continue
        try:
            providers[name] = cls(api_key=api_key)
        except Exception as e:
            from loguru import logger
            logger.warning(f"Failed to load provider {name}: {e}")

    return providers