from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from loguru import logger

# base.py has no optional deps — always safe to import eagerly
from .base import BaseProvider, CompletionRequest, CompletionResponse, Message

if TYPE_CHECKING:
    # Only used by type checkers (mypy/pyright); never executed at runtime
    from .openai    import OpenAIProvider
    from .anthropic import AnthropicProvider
    from .gemini    import GeminiProvider
    from .grok      import GrokProvider
    from .deepseek  import DeepSeekProvider

__all__ = [
    "BaseProvider",
    "CompletionRequest",
    "CompletionResponse",
    "Message",
    "PROVIDER_MAP",
    "load_providers",
    "load_providers_or_raise",
    "ProviderLoadResult",
]


def _lazy_import(provider_name: str) -> type:
    """
    Import and return the provider class for *provider_name*.
    Raises ImportError with a helpful message if the SDK is not installed.
    """
    try:
        if provider_name == "openai":
            from .openai import OpenAIProvider
            return OpenAIProvider
        if provider_name == "anthropic":
            from .anthropic import AnthropicProvider
            return AnthropicProvider
        if provider_name == "gemini":
            from .gemini import GeminiProvider
            return GeminiProvider
        if provider_name == "grok":
            from .grok import GrokProvider
            return GrokProvider
        if provider_name == "deepseek":
            from .deepseek import DeepSeekProvider
            return DeepSeekProvider
    except ImportError as exc:
        raise ImportError(
            f"Provider '{provider_name}' requires a package that is not installed: "
            f"{exc}.  Run: pip install -r requirements.txt"
        ) from exc
    raise KeyError(f"Unknown provider name: '{provider_name}'")


# ---------------------------------------------------------------------------
# Single source of truth for provider → env-var mapping.
# The provider *class* is resolved lazily via _lazy_import() so that tests
# which mock providers never trigger SDK imports.
# ---------------------------------------------------------------------------

PROVIDER_MAP: dict[str, str] = {
    "openai":    "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini":    "GEMINI_API_KEY",
    "grok":      "GROK_API_KEY",
    "deepseek":  "DEEPSEEK_API_KEY",
}


@dataclass
class ProviderLoadResult:
    """Result of a load_providers() call with per-provider diagnostics."""
    providers: dict[str, BaseProvider] = field(default_factory=dict)
    skipped:   dict[str, str]          = field(default_factory=dict)  # name → reason
    failed:    dict[str, str]          = field(default_factory=dict)  # name → error

    def __bool__(self) -> bool:
        return bool(self.providers)

    def summary(self) -> str:
        lines = [f"Loaded: {sorted(self.providers)}"]
        if self.skipped:
            lines.append(f"Skipped (no key): {sorted(self.skipped)}")
        if self.failed:
            lines.append(f"Failed: {self.failed}")
        return " | ".join(lines)


def load_providers(
    enabled: list[str] | None = None,
) -> ProviderLoadResult:
    """
    Attempt to instantiate every provider whose API key is present in the
    environment.  Returns a ProviderLoadResult — never raises.

    Parameters
    ----------
    enabled:
        When given, only these provider names are considered.
        Unknown names are warned and skipped.
    """
    result  = ProviderLoadResult()
    targets = enabled if enabled is not None else list(PROVIDER_MAP.keys())

    for name in targets:
        if name not in PROVIDER_MAP:
            logger.warning(f"load_providers: unknown provider '{name}' — skipping")
            continue

        env_var = PROVIDER_MAP[name]
        api_key = os.getenv(env_var, "").strip()

        if not api_key:
            result.skipped[name] = f"{env_var} not set"
            logger.debug(f"  [SKIP] {name} — {env_var} not set")
            continue

        try:
            cls = _lazy_import(name)
            result.providers[name] = cls(api_key=api_key)
            logger.info(f"  [OK]   {name}")
        except ImportError as exc:
            result.failed[name] = str(exc)
            logger.warning(f"  [FAIL] {name} — SDK not installed: {exc}")
        except Exception as exc:
            result.failed[name] = str(exc)
            logger.warning(f"  [FAIL] {name} — init error: {exc}")

    return result


def load_providers_or_raise(
    enabled:  list[str] | None = None,
    required: list[str] | None = None,
) -> dict[str, BaseProvider]:
    """
    Like load_providers() but raises RuntimeError when:
      - No providers loaded at all, OR
      - Any name listed in `required` failed to load.

    Returns the providers dict directly for callers that don't need
    the full ProviderLoadResult.
    """
    result = load_providers(enabled)

    if required:
        missing = [n for n in required if n not in result.providers]
        if missing:
            details = {
                **{n: result.skipped[n] for n in missing if n in result.skipped},
                **{n: result.failed[n]  for n in missing if n in result.failed},
            }
            raise RuntimeError(
                f"Required providers could not be loaded: {missing}\n"
                f"Details: {details}"
            )

    if not result.providers:
        env_vars = list(PROVIDER_MAP.values())
        raise RuntimeError(
            "No AI providers loaded.  Set at least one of:\n"
            + "\n".join(f"  {v}" for v in env_vars)
        )

    logger.info(f"Providers ready: {result.summary()}")
    return result.providers