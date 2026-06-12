import time
from typing import Optional
from enum import Enum

from loguru import logger

from providers import BaseProvider, CompletionRequest, CompletionResponse, Message
from .cache import ResponseCache
from .retry import RetryManager, RetryConfig
from .limiter import GlobalRateLimiter, RateLimitConfig


class RoutingStrategy(str, Enum):
    CHEAPEST = "cheapest"  # Route to cheapest model
    FASTEST = "fastest"  # Route to fastest provider
    BALANCED = "balanced"  # Balance cost and speed
    ROUND_ROBIN = "round_robin"  # Rotate through providers
    FALLBACK = "fallback"  # Primary with fallbacks


class RoutingConfig:
    def __init__(
        self,
        strategy: RoutingStrategy = RoutingStrategy.BALANCED,
        enable_cache: bool = True,
        cache_ttl_seconds: int = 3600,
        enable_retry: bool = True,
        max_retry_attempts: int = 3,
    ):
        self.strategy = strategy
        self.enable_cache = enable_cache
        self.cache_ttl_seconds = cache_ttl_seconds
        self.enable_retry = enable_retry
        self.max_retry_attempts = max_retry_attempts


class Router:
    """Intelligent router that selects providers based on strategy"""

    def __init__(
        self,
        providers: dict[str, BaseProvider],
        config: RoutingConfig = None,
    ):
        self.providers = providers
        self.config = config or RoutingConfig()

        # Initialize supporting components
        self.cache = ResponseCache() if self.config.enable_cache else None
        self.retry_manager = (
            RetryManager(RetryConfig(max_attempts=self.config.max_retry_attempts))
            if self.config.enable_retry
            else None
        )
        self.limiter = GlobalRateLimiter()

        # Register rate limits for each provider
        for name in providers.keys():
            self.limiter.register_provider(name, RateLimitConfig())

        # Routing state
        self.request_count = 0
        self.round_robin_index = 0
        self.provider_stats = {name: {"requests": 0, "total_cost": 0, "errors": 0} for name in providers.keys()}

        logger.info(f"Router initialized with {len(providers)} providers, strategy: {self.config.strategy}")

    async def route(
        self,
        request: CompletionRequest,
        preferred_provider: Optional[str] = None,
    ) -> CompletionResponse:
        """Route request to best provider based on strategy"""
        self.request_count += 1

        # Check cache first
        if self.cache and not request.stream:
            messages_dict = [{"role": m.role, "content": m.content} for m in request.messages]
            cached = await self.cache.get(
                provider="*",  # Cache is provider-agnostic
                model=request.model or "*",
                messages=messages_dict,
            )
            if cached:
                logger.info(f"Returning cached response")
                return CompletionResponse(**cached)

        # Select provider
        provider_name = preferred_provider or self._select_provider(request)
        provider = self.providers.get(provider_name)
        if not provider:
            raise ValueError(f"Provider {provider_name} not available")

        # Acquire rate limit slot
        estimated_tokens = self._estimate_tokens(request)
        await self.limiter.acquire(provider_name, estimated_tokens)

        try:
            # Execute with retry if enabled
            if self.config.enable_retry and self.retry_manager:
                response = await self.retry_manager.execute(
                    provider.complete,
                    request,
                )
            else:
                response = await provider.complete(request)

            # Cache response
            if self.cache and not request.stream:
                messages_dict = [{"role": m.role, "content": m.content} for m in request.messages]
                await self.cache.set(
                    provider="*",
                    model=request.model or "*",
                    messages=messages_dict,
                    response=response.__dict__,
                    ttl_seconds=self.config.cache_ttl_seconds,
                )

            # Update stats
            self.provider_stats[provider_name]["requests"] += 1
            self.provider_stats[provider_name]["total_cost"] += response.cost_usd

            logger.info(
                f"[{provider_name}] {response.model} | "
                f"{response.input_tokens}in {response.output_tokens}out | "
                f"${response.cost_usd:.6f} | {response.latency_ms:.0f}ms"
            )

            return response

        except Exception as e:
            self.provider_stats[provider_name]["errors"] += 1
            logger.error(f"[{provider_name}] Request failed: {str(e)[:100]}")
            raise

        finally:
            self.limiter.release(provider_name)

    def _select_provider(self, request: CompletionRequest) -> str:
        """Select provider based on routing strategy"""
        available = list(self.providers.keys())
        if not available:
            raise ValueError("No providers available")

        if self.config.strategy == RoutingStrategy.CHEAPEST:
            return self._select_cheapest(request, available)
        elif self.config.strategy == RoutingStrategy.FASTEST:
            return self._select_fastest(available)
        elif self.config.strategy == RoutingStrategy.BALANCED:
            return self._select_balanced(request, available)
        elif self.config.strategy == RoutingStrategy.ROUND_ROBIN:
            return self._select_round_robin(available)
        else:
            return available[0]

    def _select_cheapest(self, request: CompletionRequest, available: list[str]) -> str:
        """Select cheapest provider for estimated tokens"""
        estimated_tokens = self._estimate_tokens(request)
        best_provider = None
        best_cost = float("inf")

        for name in available:
            provider = self.providers[name]
            cost = provider.estimate_cost(estimated_tokens, estimated_tokens, request.model or provider.default_model)
            if cost < best_cost:
                best_cost = cost
                best_provider = name

        logger.debug(f"Selected {best_provider} (cheapest: ${best_cost:.6f})")
        return best_provider or available[0]

    def _select_fastest(self, available: list[str]) -> str:
        """Select fastest provider (round-robin for now)"""
        provider = available[self.round_robin_index % len(available)]
        self.round_robin_index += 1
        return provider

    def _select_balanced(self, request: CompletionRequest, available: list[str]) -> str:
        """Balance cost and speed - prefer cheapest if multiple available"""
        return self._select_cheapest(request, available)

    def _select_round_robin(self, available: list[str]) -> str:
        """Rotate through providers"""
        provider = available[self.round_robin_index % len(available)]
        self.round_robin_index += 1
        return provider

    def _estimate_tokens(self, request: CompletionRequest) -> int:
        """Rough estimate of tokens for rate limiting"""
        total_chars = sum(len(m.content) for m in request.messages)
        return max(100, total_chars // 4)  # ~4 chars per token

    def stats(self) -> dict:
        return {
            "total_requests": self.request_count,
            "strategy": self.config.strategy,
            "provider_stats": self.provider_stats,
            "cache_stats": self.cache.stats() if self.cache else None,
            "retry_stats": self.retry_manager.stats() if self.retry_manager else None,
            "limiter_stats": self.limiter.stats(),
        }