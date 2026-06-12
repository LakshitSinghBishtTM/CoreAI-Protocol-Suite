"""
CoreAI Protocol Suite - Bootloader
Handles startup sequencing, config validation, and provider initialization.
"""

import os
import sys
import asyncio
from typing import Optional
from dataclasses import dataclass

from loguru import logger

from providers import (
    BaseProvider,
    OpenAIProvider,
    AnthropicProvider,
    GeminiProvider,
    GrokProvider,
    DeepSeekProvider,
)
from .router import Router, RoutingConfig, RoutingStrategy
from .orchestrator import Orchestrator


PROVIDER_MAP = {
    "openai": (OpenAIProvider, "OPENAI_API_KEY"),
    "anthropic": (AnthropicProvider, "ANTHROPIC_API_KEY"),
    "gemini": (GeminiProvider, "GEMINI_API_KEY"),
    "grok": (GrokProvider, "GROK_API_KEY"),
    "deepseek": (DeepSeekProvider, "DEEPSEEK_API_KEY"),
}


@dataclass
class BootConfig:
    strategy: RoutingStrategy = RoutingStrategy.BALANCED
    enable_cache: bool = True
    enable_retry: bool = True
    required_providers: list = None  # None = load all available


class BootError(Exception):
    pass


def _load_providers(required: Optional[list] = None) -> dict[str, BaseProvider]:
    """
    Load providers based on available API keys.
    If required list given, raises BootError if any are missing.
    """
    providers = {}
    missing = []

    for name, (cls, env_var) in PROVIDER_MAP.items():
        api_key = os.getenv(env_var)
        if api_key:
            try:
                providers[name] = cls(api_key=api_key)
                logger.info(f"  [OK] {name}")
            except Exception as e:
                logger.warning(f"  [FAIL] {name}: {e}")
        else:
            if required and name in required:
                missing.append(name)
            else:
                logger.debug(f"  [SKIP] {name} ({env_var} not set)")

    if missing:
        raise BootError(
            f"Required providers missing API keys: {', '.join(missing)}\n"
            f"Set the corresponding environment variables and retry."
        )

    return providers


def _validate_env():
    """Warn about missing optional config vars."""
    optional = {
        "REDIS_URL": "caching will use in-memory backend",
        "LOG_LEVEL": "defaulting to INFO",
        "SERVER_PORT": "defaulting to 6969",
    }
    for var, note in optional.items():
        if not os.getenv(var):
            logger.debug(f"  {var} not set — {note}")


def boot(config: BootConfig = None) -> tuple[Router, Orchestrator]:
    """
    Full system boot sequence.
    Returns initialized (Router, Orchestrator) ready for use.

    Raises BootError on unrecoverable startup failures.
    """
    config = config or BootConfig()

    logger.info("=" * 50)
    logger.info("CoreAI Protocol Suite — Booting")
    logger.info("=" * 50)

    # 1. Environment check
    logger.info("Step 1/4 — Validating environment")
    _validate_env()

    # 2. Load providers
    logger.info("Step 2/4 — Loading providers")
    providers = _load_providers(required=config.required_providers)

    if not providers:
        raise BootError(
            "No providers loaded. Set at least one API key:\n"
            + "\n".join(f"  {env}" for _, env in PROVIDER_MAP.values())
        )

    logger.info(f"  Loaded {len(providers)} provider(s): {', '.join(providers)}")

    # 3. Initialize router
    logger.info("Step 3/4 — Initializing router")
    routing_config = RoutingConfig(
        strategy=config.strategy,
        enable_cache=config.enable_cache,
        enable_retry=config.enable_retry,
    )
    router = Router(providers=providers, config=routing_config)
    logger.info(f"  Strategy: {config.strategy.value}")
    logger.info(f"  Cache: {'enabled' if config.enable_cache else 'disabled'}")
    logger.info(f"  Retry: {'enabled' if config.enable_retry else 'disabled'}")

    # 4. Initialize orchestrator
    logger.info("Step 4/4 — Initializing orchestrator")
    orchestrator = Orchestrator()

    logger.info("=" * 50)
    logger.info("Boot complete.")
    logger.info("=" * 50)

    return router, orchestrator


def boot_minimal() -> tuple[Router, Orchestrator]:
    """Quick boot with defaults — for scripts and testing."""
    return boot(BootConfig())
