import time
from typing import Optional
from enum import Enum

from loguru import logger

from providers import BaseProvider, CompletionRequest, CompletionResponse
from .cache import ResponseCache
from .retry import RetryManager, RetryConfig
from .limiter import GlobalRateLimiter, RateLimitConfig

# ---------------------------------------------------------------------------
# Strategy weights used by BALANCED
# ---------------------------------------------------------------------------

_COST_WEIGHT = 0.70
_LATENCY_WEIGHT = 0.30

# How many recent latency samples to keep per provider
_LATENCY_HISTORY_SIZE = 20

# Minimum samples required before a provider competes on latency.
# Until every provider has this many samples, under-sampled providers
# are explored via round-robin so no provider is starved during warm-up.
_MIN_SAMPLES = 3


class RoutingStrategy(str, Enum):
    CHEAPEST = "cheapest"  # lowest estimated cost
    FASTEST = "fastest"  # lowest recent p50 latency
    BALANCED = "balanced"  # weighted cost + latency score
    ROUND_ROBIN = "round_robin"  # equal distribution, no preferences
    FALLBACK = "fallback"  # first in list, falls back on error


class RoutingConfig:
    def __init__(
        self,
        strategy: RoutingStrategy = RoutingStrategy.BALANCED,
        enable_cache: bool = True,
        cache_ttl_seconds: int = 3600,
        enable_retry: bool = True,
        max_retry_attempts: int = 3,
        cost_weight: float = _COST_WEIGHT,
        latency_weight: float = _LATENCY_WEIGHT,
    ):
        self.strategy = strategy
        self.enable_cache = enable_cache
        self.cache_ttl_seconds = cache_ttl_seconds
        self.enable_retry = enable_retry
        self.max_retry_attempts = max_retry_attempts
        # Balanced weights — must sum to 1.0
        total = cost_weight + latency_weight
        self.cost_weight = cost_weight / total
        self.latency_weight = latency_weight / total


class Router:
    """Intelligent router that selects providers based on strategy."""

    def __init__(
        self,
        providers: dict[str, BaseProvider],
        config: RoutingConfig = None,
    ):
        self.providers = providers
        self.config = config or RoutingConfig()

        # Supporting components
        self.cache = ResponseCache() if self.config.enable_cache else None
        self.retry_manager = (
            RetryManager(RetryConfig(max_attempts=self.config.max_retry_attempts))
            if self.config.enable_retry
            else None
        )
        self.limiter = GlobalRateLimiter()
        for name in providers:
            self.limiter.register_provider(name, RateLimitConfig())

        # Routing state
        self.request_count = 0
        self._rr_index = 0  # only used by round-robin
        self.provider_stats: dict[str, dict] = {
            name: {
                "requests": 0,
                "errors": 0,
                "total_cost": 0.0,
                "latency_ms_history": [],  # rolling window
            }
            for name in providers
        }

        logger.info(
            f"Router initialised — {len(providers)} provider(s), "
            f"strategy: {self.config.strategy.value}"
        )

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    async def route(
        self,
        request: CompletionRequest,
        preferred_provider: Optional[str] = None,
    ) -> CompletionResponse:
        """Route a request to the best available provider."""
        self.request_count += 1

        # Validate explicit provider choice up front so the error is clear.
        if preferred_provider is not None and preferred_provider not in self.providers:
            available = sorted(self.providers.keys())
            raise ValueError(
                f"Provider '{preferred_provider}' not available. "
                f"Available: {available}"
            )

        provider_name = preferred_provider or self._select_provider(request)

        # Cache lookup — key includes provider_name so explicit-provider
        # requests are never satisfied from a different provider's cache.
        if self.cache and not request.stream:
            messages_dict = [
                {"role": m.role, "content": m.content} for m in request.messages
            ]
            cached = await self.cache.get(
                provider_name, request.model or "*", messages_dict
            )
            if cached:
                logger.debug(f"Cache HIT for provider={provider_name}")
                return CompletionResponse(**cached)

        provider = self.providers[provider_name]
        estimated_tokens = self._estimate_tokens(request)
        await self.limiter.acquire(provider_name, estimated_tokens)

        t_start = time.perf_counter()
        try:
            if self.config.enable_retry and self.retry_manager:
                response = await self.retry_manager.execute(provider.complete, request)
            else:
                response = await provider.complete(request)

            latency_ms = (time.perf_counter() - t_start) * 1000

            # Store in cache
            if self.cache and not request.stream:
                messages_dict = [
                    {"role": m.role, "content": m.content} for m in request.messages
                ]
                await self.cache.set(
                    provider_name,
                    request.model or "*",
                    messages_dict,
                    response.__dict__,
                    ttl_seconds=self.config.cache_ttl_seconds,
                )

            # Update stats
            self._record_success(provider_name, response.cost_usd, latency_ms)

            logger.info(
                f"[{provider_name}] {response.model} | "
                f"{response.input_tokens}in {response.output_tokens}out | "
                f"${response.cost_usd:.6f} | {response.latency_ms:.0f}ms"
            )
            return response

        except Exception as e:
            self._record_error(provider_name)
            logger.error(f"[{provider_name}] Request failed: {str(e)[:120]}")
            raise

        finally:
            self.limiter.release(provider_name)

    # ------------------------------------------------------------------ #
    # Provider selection
    # ------------------------------------------------------------------ #

    def _select_provider(self, request: CompletionRequest) -> str:
        available = list(self.providers.keys())
        if not available:
            raise ValueError("No providers available")

        strategy = self.config.strategy
        if strategy == RoutingStrategy.CHEAPEST:
            return self._select_cheapest(request, available)
        if strategy == RoutingStrategy.FASTEST:
            return self._select_fastest(available)
        if strategy == RoutingStrategy.BALANCED:
            return self._select_balanced(request, available)
        if strategy == RoutingStrategy.ROUND_ROBIN:
            return self._select_round_robin(available)
        # FALLBACK: first provider wins; caller handles errors / retries
        return available[0]

    def _select_cheapest(self, request: CompletionRequest, available: list[str]) -> str:
        """Return the provider with the lowest estimated cost."""
        estimated = self._estimate_tokens(request)
        best, best_cost = available[0], float("inf")
        for name in available:
            p = self.providers[name]
            cost = p.estimate_cost(
                estimated, estimated, request.model or p.default_model
            )
            if cost < best_cost:
                best_cost = cost
                best = name
        logger.debug(f"Cheapest provider: {best} (${best_cost:.6f})")
        return best

    def _select_fastest(self, available: list[str]) -> str:
        """
        Return the provider with the lowest recent p50 latency.

        Warm-up strategy: any provider with fewer than _MIN_SAMPLES observations
        is considered under-explored and gets priority via round-robin before
        latency-based selection kicks in.  This prevents the first provider
        sampled from monopolising all traffic while others are starved.
        """
        # Identify providers that still need more samples
        under_sampled = [
            name
            for name in available
            if len(self.provider_stats[name]["latency_ms_history"]) < _MIN_SAMPLES
        ]
        if under_sampled:
            # Explore under-sampled providers in round-robin order
            chosen = under_sampled[self._rr_index % len(under_sampled)]
            self._rr_index += 1
            logger.debug(
                f"Fastest warm-up: exploring {chosen} "
                f"({len(self.provider_stats[chosen]['latency_ms_history'])}/{_MIN_SAMPLES} samples)"
            )
            return chosen

        # All providers have enough history — pick the true p50 winner
        best, best_latency = available[0], float("inf")
        for name in available:
            history = self.provider_stats[name]["latency_ms_history"]
            p50 = sorted(history)[len(history) // 2]
            if p50 < best_latency:
                best_latency = p50
                best = name

        logger.debug(f"Fastest provider: {best} (p50={best_latency:.0f}ms)")
        return best

    def _select_balanced(self, request: CompletionRequest, available: list[str]) -> str:
        """
        Score each provider by a weighted combination of normalised cost
        and normalised latency (lower is better for both).

        score = cost_weight * norm_cost + latency_weight * norm_latency

        The provider with the lowest score wins.
        """
        estimated = self._estimate_tokens(request)

        raw_costs: dict[str, float] = {}
        raw_latencies: dict[str, float] = {}

        for name in available:
            p = self.providers[name]
            raw_costs[name] = p.estimate_cost(
                estimated, estimated, request.model or p.default_model
            )
            history = self.provider_stats[name]["latency_ms_history"]
            raw_latencies[name] = (
                sorted(history)[len(history) // 2] if history else 500.0
            )

        # Normalise to [0, 1] — avoid division by zero
        max_cost = max(raw_costs.values()) or 1.0
        max_latency = max(raw_latencies.values()) or 1.0

        best, best_score = available[0], float("inf")
        for name in available:
            norm_cost = raw_costs[name] / max_cost
            norm_latency = raw_latencies[name] / max_latency
            score = (
                self.config.cost_weight * norm_cost
                + self.config.latency_weight * norm_latency
            )
            if score < best_score:
                best_score = score
                best = name

        logger.debug(f"Balanced provider: {best} (score={best_score:.4f})")
        return best

    def _select_round_robin(self, available: list[str]) -> str:
        """Cycle through providers in order, wrapping around."""
        provider = available[self._rr_index % len(available)]
        self._rr_index += 1
        return provider

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _estimate_tokens(self, request: CompletionRequest) -> int:
        total_chars = sum(len(m.content) for m in request.messages)
        return max(100, total_chars // 4)

    def _record_success(self, name: str, cost_usd: float, latency_ms: float):
        stats = self.provider_stats[name]
        stats["requests"] += 1
        stats["total_cost"] += cost_usd
        history = stats["latency_ms_history"]
        history.append(latency_ms)
        if len(history) > _LATENCY_HISTORY_SIZE:
            history.pop(0)

    def _record_error(self, name: str):
        self.provider_stats[name]["errors"] += 1

    def _p50_latency(self, name: str) -> Optional[float]:
        history = self.provider_stats[name]["latency_ms_history"]
        if not history:
            return None
        return sorted(history)[len(history) // 2]

    def stats(self) -> dict:
        return {
            "total_requests": self.request_count,
            "strategy": self.config.strategy,
            "provider_stats": {
                name: {
                    "requests": s["requests"],
                    "errors": s["errors"],
                    "total_cost": s["total_cost"],
                    "p50_latency_ms": self._p50_latency(name),
                }
                for name, s in self.provider_stats.items()
            },
            "cache_stats": self.cache.stats() if self.cache else None,
            "retry_stats": self.retry_manager.stats() if self.retry_manager else None,
            "limiter_stats": self.limiter.stats(),
        }
