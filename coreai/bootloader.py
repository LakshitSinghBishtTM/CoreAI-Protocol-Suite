import os
from dataclasses import dataclass, field

from loguru import logger

from providers import load_providers_or_raise
from .router import Router, RoutingConfig, RoutingStrategy
from .orchestrator import Orchestrator

# Flags that should never be true in production
_DANGEROUS_FLAGS: dict[str, str] = {
    "SKIP_AUTH_CHECKS": "authentication is disabled",
    "ALLOW_UNVERIFIED_REQUESTS": "unverified requests are accepted",
    "ALLOW_REMOTE_EXECUTION": "arbitrary remote execution is enabled",
    "ALLOW_FILE_SYSTEM_ACCESS": "unrestricted filesystem access is on",
    "DEBUG_MODE": "debug mode leaks internals",
}


@dataclass
class BootConfig:
    strategy: RoutingStrategy = RoutingStrategy.BALANCED
    enable_cache: bool = True
    enable_retry: bool = True
    required_providers: list[str] = field(default_factory=list)
    enabled_providers: list[str] = field(default_factory=list)
    cost_weight: float = 0.70
    latency_weight: float = 0.30


class BootError(Exception):
    pass


def _validate_env(strict: bool = False) -> None:
    """
    Warn about missing optional config and dangerous feature flags.

    Parameters
    ----------
    strict:
        When True, raise BootError if any dangerous flag is set to 'true'.
        Useful for production health checks.
    """
    optional = {
        "REDIS_URL": "caching will use in-memory backend",
        "LOG_LEVEL": "defaulting to INFO",
        "SERVER_PORT": "defaulting to 6389",
    }
    for var, note in optional.items():
        if not os.getenv(var):
            logger.debug(f"  {var} not set — {note}")

    env = os.getenv("ENVIRONMENT", "development").lower()
    if env == "production":
        for flag, description in _DANGEROUS_FLAGS.items():
            value = os.getenv(flag, "false").lower()
            if value in ("true", "1", "yes"):
                msg = f"Dangerous flag {flag}=true in production — {description}"
                if strict:
                    raise BootError(msg)
                logger.warning(f"  WARNING: {msg}")


def boot(config: BootConfig = None) -> tuple[Router, Orchestrator]:
    """
    Full system boot sequence.  Returns (Router, Orchestrator).

    Raises BootError on unrecoverable startup failures.
    """
    config = config or BootConfig()

    logger.info("=" * 52)
    logger.info("CoreAI Protocol Suite — Booting")
    logger.info("=" * 52)

    # 1. Environment validation
    logger.info("Step 1/4 — Validating environment")
    _validate_env()

    # 2. Load providers
    logger.info("Step 2/4 — Loading providers")
    try:
        providers = load_providers_or_raise(
            enabled=config.enabled_providers or None,
            required=config.required_providers or None,
        )
    except RuntimeError as exc:
        raise BootError(str(exc)) from exc

    logger.info(
        f"  Loaded {len(providers)} provider(s): {', '.join(sorted(providers))}"
    )

    # 3. Build router
    logger.info("Step 3/4 — Initialising router")
    routing_config = RoutingConfig(
        strategy=config.strategy,
        enable_cache=config.enable_cache,
        enable_retry=config.enable_retry,
        cost_weight=config.cost_weight,
        latency_weight=config.latency_weight,
    )
    router = Router(providers=providers, config=routing_config)
    logger.info(f"  Strategy:       {config.strategy.value}")
    logger.info(f"  Cost weight:    {routing_config.cost_weight:.2f}")
    logger.info(f"  Latency weight: {routing_config.latency_weight:.2f}")
    logger.info(f"  Cache:          {'enabled' if config.enable_cache else 'disabled'}")
    logger.info(f"  Retry:          {'enabled' if config.enable_retry else 'disabled'}")

    # 4. Orchestrator
    logger.info("Step 4/4 — Initialising orchestrator")
    orchestrator = Orchestrator()

    logger.info("=" * 52)
    logger.info("Boot complete.")
    logger.info("=" * 52)

    return router, orchestrator


def boot_minimal() -> tuple[Router, Orchestrator]:
    """Quick boot with defaults — for scripts and one-off completions."""
    return boot(BootConfig())
