"""
providers/__init__.py
"""

import os
from dataclasses import dataclass, field

from loguru import logger

# Explicit submodule imports so patch("providers.openai.OpenAIProvider") works
from . import anthropic, deepseek, gemini, grok, openai
from .anthropic import AnthropicProvider
from .base import (
    BaseProvider,
    CompletionRequest,
    CompletionResponse,
    Message,
    StreamUsage,
)
from .deepseek import DeepSeekProvider
from .gemini import GeminiProvider
from .grok import GrokProvider
from .openai import OpenAIProvider

__all__ = [
    "BaseProvider",
    "CompletionRequest",
    "StreamUsage",
    "CompletionResponse",
    "Message",
    "OpenAIProvider",
    "AnthropicProvider",
    "GeminiProvider",
    "GrokProvider",
    "DeepSeekProvider",
    "ProviderLoadResult",
    "PROVIDER_MAP",
    "load_providers",
    "load_providers_or_raise",
]

PROVIDER_MAP: dict[str, tuple] = {
    "openai": (OpenAIProvider, "OPENAI_API_KEY"),
    "anthropic": (AnthropicProvider, "ANTHROPIC_API_KEY"),
    "gemini": (GeminiProvider, "GEMINI_API_KEY"),
    "grok": (GrokProvider, "GROK_API_KEY"),
    "deepseek": (DeepSeekProvider, "DEEPSEEK_API_KEY"),
}


@dataclass
class ProviderLoadResult:
    providers: dict[str, BaseProvider] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)

    # Dict-like interface so existing call sites work without modification.
    # Tests assert: result == {}, "openai" in result, result["openai"]
    def __contains__(self, item):
        return item in self.providers

    def __iter__(self):
        return iter(self.providers)

    def __getitem__(self, key):
        return self.providers[key]

    def __len__(self):
        return len(self.providers)

    def __bool__(self):
        return bool(self.providers)

    def __eq__(self, other):
        if isinstance(other, dict):
            return self.providers == other
        if isinstance(other, ProviderLoadResult):
            return (
                self.providers == other.providers
                and self.skipped == other.skipped
                and self.failed == other.failed
            )
        return NotImplemented

    def keys(self):
        return self.providers.keys()

    def values(self):
        return self.providers.values()

    def items(self):
        return self.providers.items()

    def get(self, key, default=None):
        return self.providers.get(key, default)


def load_providers(
    enabled: list[str] | None = None,
) -> ProviderLoadResult:
    """
    Load all providers that have API keys set in env.
    Returns ProviderLoadResult with .providers, .skipped, .failed.
    """
    result = ProviderLoadResult()
    targets = list(enabled) if enabled is not None else list(PROVIDER_MAP.keys())

    for name in targets:
        if name not in PROVIDER_MAP:
            result.skipped[name] = f"unknown provider '{name}'"
            continue

        cls, env_var = PROVIDER_MAP[name]
        api_key = os.getenv(str(env_var), "").strip()

        if not api_key:
            reason = f"{env_var} not set"
            logger.debug(f"  [SKIP] {name} — {reason}")
            result.skipped[name] = reason
            continue

        try:
            result.providers[name] = cls(api_key=api_key)
            logger.info(f"  [OK]   {name} loaded")
        except Exception as e:
            reason = str(e)
            logger.warning(f"  [FAIL] {name} — {reason}")
            result.failed[name] = reason

    if enabled is None:
        _load_local(result)
    return result


def _load_local(result: "ProviderLoadResult") -> None:
    """Add self-hosted servers from config/local.yml (or $COREAI_LOCAL_FILE)."""
    path = os.getenv("COREAI_LOCAL_FILE", "config/local.yml")
    if not os.path.exists(path):
        return
    try:
        import yaml

        from .local import build_local_providers

        with open(path) as fh:
            built = build_local_providers(yaml.safe_load(fh) or {})
    except Exception as e:
        logger.warning(f"  [FAIL] local servers ({path}) — {e}")
        result.failed["local"] = str(e)
        return
    built.update(_managed(path))
    for name, provider in built.items():
        if name in result.providers:
            result.failed[name] = "name clashes with a cloud provider"
            continue
        result.providers[name] = provider
        logger.info(f"  [OK]   {name} (local, {provider.base_url})")


def _managed(path: str) -> dict:
    """Optional ``managed:`` block in local.yml: a models dir run via llama-server."""
    import yaml

    with open(path) as fh:
        cfg = (yaml.safe_load(fh) or {}).get("managed")
    if not cfg:
        return {}
    from localrt.provider import ManagedLocalProvider
    from localrt.registry import ModelRegistry
    from localrt.supervisor import Supervisor

    name = cfg.get("name", "gguf")
    sup = Supervisor(
        max_loaded=cfg.get("max_loaded", 1),
        idle_timeout_s=cfg.get("idle_timeout_s", 600),
    )
    prov = ManagedLocalProvider(
        name,
        ModelRegistry(cfg.get("models_dir", "models"), cfg.get("capabilities")),
        sup,
        binary=cfg.get("binary", ""),
        max_queue=cfg.get("max_queue", 0),
    )
    return {name: prov}


def load_providers_or_raise(
    enabled: list[str] | None = None,
    required: list[str] | None = None,
) -> dict[str, BaseProvider]:
    """
    Like load_providers() but raises RuntimeError if no providers loaded
    or any provider in `required` is missing. Returns plain providers dict.
    """
    result = load_providers(enabled)

    if required:
        missing = [r for r in required if r not in result.providers]
        if missing:
            raise RuntimeError(
                f"Required provider(s) not available: {', '.join(missing)}. "
                "Check API keys: "
                + ", ".join(PROVIDER_MAP[r][1] for r in missing if r in PROVIDER_MAP)
            )

    if not result.providers:
        raise RuntimeError(
            "No AI providers could be loaded. "
            f"Skipped: {result.skipped}. Failed: {result.failed}."
        )

    return result.providers
