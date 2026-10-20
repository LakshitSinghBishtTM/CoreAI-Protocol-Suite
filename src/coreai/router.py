import asyncio
import dataclasses
import os
import time
from enum import Enum
from typing import Any, AsyncGenerator, Callable, Optional

from loguru import logger

from coreai import tracing
from coreai.pricing import get_pricing
from neural.model import Modality, get_registry
from providers import (
    BaseProvider,
    CompletionRequest,
    CompletionResponse,
    StreamUsage,
)
from providers.base import request_images, validate_conversation

from .budgets import BudgetExceeded
from .cache import ResponseCache
from .circuit_breaker import CircuitBreaker
from .deadline import Deadline
from .errors import (
    AllProvidersUnavailable,
    CircuitOpenError,
    DeadlineExceeded,
    ErrorKind,
    ProviderTimeout,
    RateLimited,
    classify,
)
from .health import HealthTracker
from .limiter import GlobalRateLimiter, RateLimitConfig
from .policy import Policy, PolicyStore
from .retry import RetryConfig, RetryManager

# ---------------------------------------------------------------------------
# Strategy weights used by BALANCED
# ---------------------------------------------------------------------------

_COST_WEIGHT = 0.70
_LATENCY_WEIGHT = 0.30

# How many recent latency samples to keep per provider
# Upper bounds (ms) of latency histogram buckets; one more bucket holds +Inf.
LATENCY_BUCKETS_MS = (50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000)
_MAX_MODELS_PER_PROVIDER = 50
# Rough size of one image in prompt characters (about 1000 tokens), for the
# context-window check and cost ranking.
_IMAGE_CHARS = 4000
_LATENCY_HISTORY_SIZE = 20

# Minimum samples required before a provider competes on latency.
# Until every provider has this many samples, under-sampled providers
# are explored via round-robin so no provider is starved during warm-up.
_MIN_SAMPLES = 3

# Per-attempt timeout for one provider call, and per-provider overrides.
# Slow providers (reasoning models) get longer; the request deadline still caps
# the total time across retries and failovers.
_DEFAULT_REQUEST_TIMEOUT_S = 60.0
_DEFAULT_PROVIDER_TIMEOUTS = {"deepseek": 120.0}
_DEFAULT_DEADLINE_S = 120.0


class HedgeFailure(Exception):
    """Both legs of a hedged call failed. ``errors`` maps provider -> error."""

    def __init__(self, errors: dict, primary: str):
        self.errors = errors
        self.primary = primary
        super().__init__(f"hedged call failed on {sorted(errors)}")


_END = object()  # marks a stream that ended before its first chunk


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
        enable_failover: bool = True,
        request_timeout_seconds: float = _DEFAULT_REQUEST_TIMEOUT_S,
        provider_timeouts: Optional[dict[str, float]] = None,
        deadline_seconds: Optional[float] = _DEFAULT_DEADLINE_S,
        max_attempts_before_failover: int = 2,
        retry_initial_wait_seconds: float = 1.0,
        retry_max_wait_seconds: float = 30.0,
        circuit_failure_threshold: int = 5,
        circuit_reset_seconds: float = 30.0,
        cache_max_entries: int = 1000,
        health_weight: float = 0.5,
        health_half_life_seconds: float = 60.0,
        hedge_after_seconds: Optional[float] = None,
        rate_limits: Optional[dict[str, RateLimitConfig]] = None,
    ):
        self.strategy = strategy
        self.enable_failover = enable_failover
        self.request_timeout_seconds = request_timeout_seconds
        self.provider_timeouts = {
            **_DEFAULT_PROVIDER_TIMEOUTS,
            **(provider_timeouts or {}),
        }
        self.deadline_seconds = deadline_seconds
        # While other providers remain, retry a failing provider only this many
        # times before moving on; the last candidate gets max_retry_attempts.
        self.max_attempts_before_failover = max_attempts_before_failover
        self.retry_initial_wait_seconds = retry_initial_wait_seconds
        self.retry_max_wait_seconds = retry_max_wait_seconds
        self.circuit_failure_threshold = circuit_failure_threshold
        self.circuit_reset_seconds = circuit_reset_seconds
        self.cache_max_entries = cache_max_entries
        # Added to the balanced score as health_weight * (1 - health): 0 turns
        # health scoring off, higher values avoid unhealthy providers harder.
        self.health_weight = health_weight
        # Hedging: if the chosen provider hasn't answered after this many
        # seconds, also ask the next one and use whichever succeeds first.
        # None (default) turns it off. A policy's hedge_after_ms overrides it.
        if hedge_after_seconds is not None and hedge_after_seconds <= 0:
            raise ValueError("hedge_after_seconds must be positive or None")
        self.hedge_after_seconds = hedge_after_seconds
        # Per-provider local rate limits (requests/tokens per minute, concurrency).
        # Providers without an entry get RateLimitConfig() defaults.
        self.rate_limits = dict(rate_limits or {})
        self.health_half_life_seconds = health_half_life_seconds
        self.enable_cache = enable_cache
        self.cache_ttl_seconds = cache_ttl_seconds
        self.enable_retry = enable_retry
        self.max_retry_attempts = max_retry_attempts
        # Balanced weights — must sum to 1.0
        total = cost_weight + latency_weight
        self.cost_weight = cost_weight / total
        self.latency_weight = latency_weight / total

    @classmethod
    def from_env(cls, env=None, **overrides) -> "RoutingConfig":
        """
        Build a config from environment variables, falling back to defaults.
        Keyword *overrides* (for example ``strategy=``) win over the environment.

          REQUEST_TIMEOUT_SECONDS       per-attempt timeout for one provider call
          PROVIDER_TIMEOUTS             per-provider overrides, e.g. deepseek=120,grok=90
          REQUEST_DEADLINE_SECONDS      total budget across retries and failovers
                                        ("none" disables the deadline)
          ENABLE_FAILOVER               true/false
          MAX_ATTEMPTS_BEFORE_FAILOVER  tries on one provider while others remain
          CIRCUIT_FAILURE_THRESHOLD     consecutive failures that open a breaker
          CIRCUIT_RESET_SECONDS         how long a breaker stays open before a probe
          CACHE_MAX_ENTRIES             size limit of the in-memory response cache
        """
        env = os.environ if env is None else env

        def read(name, cast):
            raw = env.get(name)
            if raw is None or raw.strip() == "":
                return None
            try:
                return cast(raw.strip())
            except ValueError:
                raise ValueError(
                    f"{name} must be a valid {cast.__name__}, got {raw!r}"
                ) from None

        def flag(raw: str) -> bool:
            value = raw.lower()
            if value in ("1", "true", "yes", "on"):
                return True
            if value in ("0", "false", "no", "off"):
                return False
            raise ValueError("expected true or false")

        def timeouts(raw: str) -> dict:
            result = {}
            for item in raw.split(","):
                if not item.strip():
                    continue
                name, _, seconds = item.partition("=")
                if not name.strip() or not seconds.strip():
                    raise ValueError("expected name=seconds pairs")
                result[name.strip()] = float(seconds)
            return result

        kwargs = {}
        for key, name, cast in (
            ("request_timeout_seconds", "REQUEST_TIMEOUT_SECONDS", float),
            ("max_attempts_before_failover", "MAX_ATTEMPTS_BEFORE_FAILOVER", int),
            ("circuit_failure_threshold", "CIRCUIT_FAILURE_THRESHOLD", int),
            ("circuit_reset_seconds", "CIRCUIT_RESET_SECONDS", float),
            ("cache_max_entries", "CACHE_MAX_ENTRIES", int),
        ):
            value = read(name, cast)
            if value is not None:
                kwargs[key] = value

        enabled = read("ENABLE_FAILOVER", flag)
        if enabled is not None:
            kwargs["enable_failover"] = enabled
        per_provider = read("PROVIDER_TIMEOUTS", timeouts)
        if per_provider is not None:
            kwargs["provider_timeouts"] = per_provider
        raw_deadline = (env.get("REQUEST_DEADLINE_SECONDS") or "").strip().lower()
        if raw_deadline == "none":
            kwargs["deadline_seconds"] = None
        else:
            deadline = read("REQUEST_DEADLINE_SECONDS", float)
            if deadline is not None:
                kwargs["deadline_seconds"] = deadline

        kwargs.update(overrides)
        return cls(**kwargs)


DEFAULT_ALIASES = {"fast": "low_latency", "cheap": "bulk", "best": "high_quality"}


class Router:
    """Intelligent router that selects providers based on strategy."""

    def __init__(
        self,
        providers: dict[str, BaseProvider],
        config: RoutingConfig = None,
        clock: Callable[[], float] = time.monotonic,
        policies: Optional[PolicyStore] = None,
    ):
        self.providers = providers
        self.config = config or RoutingConfig()
        # Optional declarative policies (config/routing.yml). Without a store
        # the router behaves exactly as configured by RoutingConfig.
        self.policies = policies
        self._model_registry = get_registry()
        self._clock = clock

        # Supporting components
        # Optional ``callable(model)`` that raises when a model's shared budget
        # is spent (set by the API). Called with the model actually chosen.
        self.model_gate = None
        # Per-policy rotation offsets for round_robin policies.
        self._policy_rr: dict[str, int] = {}
        # Virtual models: alias -> routing policy name. An alias only takes
        # effect while that policy exists.
        self.aliases: dict[str, str] = dict(DEFAULT_ALIASES)
        self.cache = (
            ResponseCache(max_entries=self.config.cache_max_entries)
            if self.config.enable_cache
            else None
        )
        self.retry_manager = (
            RetryManager(
                RetryConfig(
                    max_attempts=self.config.max_retry_attempts,
                    initial_wait_seconds=self.config.retry_initial_wait_seconds,
                    max_wait_seconds=self.config.retry_max_wait_seconds,
                )
            )
            if self.config.enable_retry
            else None
        )
        self.limiter = GlobalRateLimiter()
        for name in providers:
            self.limiter.register_provider(
                name, self.config.rate_limits.get(name) or RateLimitConfig()
            )
        self.breakers: dict[str, CircuitBreaker] = {
            name: CircuitBreaker(
                name,
                failure_threshold=self.config.circuit_failure_threshold,
                reset_timeout=self.config.circuit_reset_seconds,
                clock=clock,
            )
            for name in providers
        }
        self.failover_count = 0
        self.hedge_count = 0  # second requests launched
        self.hedge_wins = 0  # times the second request answered first
        self.health: dict[str, HealthTracker] = {
            name: HealthTracker(self.config.health_half_life_seconds, clock=clock)
            for name in providers
        }

        # Routing state
        self.request_count = 0
        self._rr_index = 0  # only used by round-robin
        # (provider, model) -> request/token counters and a latency histogram.
        self.model_stats: dict[tuple[str, str], dict] = {}
        # Streams whose token counts the router had to estimate, by provider.
        self.streams_estimated: dict[str, int] = {}
        self.provider_stats: dict[str, dict] = {
            name: {
                "requests": 0,
                "errors": 0,
                "total_cost": 0.0,
                "latency_ms_history": [],  # rolling window
                "error_kinds": {},  # ErrorKind value -> count
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
        *,
        deadline_seconds: Optional[float] = None,
    ) -> CompletionResponse:
        """Route a request (see ``_route``), inside a ``caips.route`` span."""
        with tracing.start_span(
            "caips.route", {"caips.preferred_provider": preferred_provider}
        ) as span:
            response = await self._route(
                request, preferred_provider, deadline_seconds=deadline_seconds
            )
            span.set_attributes(
                {
                    "caips.provider": response.provider,
                    "caips.model": response.model,
                    "caips.input_tokens": response.input_tokens,
                    "caips.output_tokens": response.output_tokens,
                    "caips.cost_usd": response.cost_usd,
                    "caips.cached": response.cached,
                    "caips.failed_over_from": response.metadata.get("failed_over_from"),
                    "caips.hedged": bool(response.metadata.get("hedged")),
                }
            )
            return response

    async def _route(
        self,
        request: CompletionRequest,
        preferred_provider: Optional[str] = None,
        *,
        deadline_seconds: Optional[float] = None,
    ) -> CompletionResponse:
        """
        Route a request to the best available provider.

        Providers with an open circuit breaker are skipped. If the chosen
        provider fails with a provider-side error (overload, 5xx, timeout,
        connection, auth, quota), the router fails over to the next-best
        provider, unless the caller pinned a provider or a specific model.
        All attempts share one deadline.
        """
        self.request_count += 1
        self.resolve_alias(request)

        # Validate explicit provider choice up front so the error is clear.
        if preferred_provider is not None and preferred_provider not in self.providers:
            available = sorted(self.providers.keys())
            raise ValueError(
                f"Provider '{preferred_provider}' not available. "
                f"Available: {available}"
            )

        validate_conversation(request.messages)
        deadline = self._new_deadline(deadline_seconds)
        # Resolve the provider and model for each candidate, then drop the
        # ones that cannot serve this request (context window, output limit,
        # streaming, per-model budget) before anything is spent. Runs before
        # the cache lookup so a cache hit cannot bypass a model's budget cap.
        screened = self._screen(request, preferred_provider)
        candidates = [n for n, _ in screened]
        reqs = [r for _, r in screened]
        provider_name = candidates[0]

        # Cache lookup — key includes provider_name so explicit-provider
        # requests are never satisfied from a different provider's cache.
        if self.cache and not request.stream and request.cacheable:
            messages_dict = [
                {"role": m.role, "content": m.content} for m in request.messages
            ]
            _t0 = time.monotonic()
            cached = await self.cache.get(
                provider_name,
                reqs[0].model or "*",
                messages_dict,
                self._cache_options(request),
            )
            if cached:
                logger.debug(f"Cache HIT for provider={provider_name}")
                hit = dict(cached)
                meta = dict(hit.get("metadata") or {})
                # No upstream call was made: report zero spend, keep the
                # original cost for reference, and flag the hit.
                meta["original_cost_usd"] = hit.get("cost_usd", 0.0)
                meta["original_latency_ms"] = hit.get("latency_ms", 0.0)
                hit.update(
                    cached=True,
                    cost_usd=0.0,
                    latency_ms=(time.monotonic() - _t0) * 1000,
                    metadata=meta,
                )
                return CompletionResponse(**hit)

        failures: list[tuple[str, ErrorKind]] = []
        last_exc: Optional[Exception] = None
        hedge_delay = self._hedge_delay(request, preferred_provider)
        skip: set[str] = set()  # providers already tried as a hedge

        for index, name in enumerate(candidates):
            if name in skip:
                continue
            is_last = index == len(candidates) - 1
            req = reqs[index]
            if not self.breakers[name].allow():
                last_exc = CircuitOpenError(name, self.breakers[name].retry_after())
                failures.append((name, ErrorKind.CIRCUIT_OPEN))
                continue

            attempts = self.config.max_retry_attempts
            if not is_last:
                attempts = min(attempts, self.config.max_attempts_before_failover)

            try:
                partner = None
                if hedge_delay is not None and not is_last and not request.stream:
                    nxt = candidates[index + 1]
                    if nxt not in skip and nxt != name:
                        partner = nxt
                if partner is not None:
                    partner_attempts = (
                        self.config.max_retry_attempts
                        if index + 1 == len(candidates) - 1
                        else min(
                            self.config.max_retry_attempts,
                            self.config.max_attempts_before_failover,
                        )
                    )
                    partner_req = reqs[index + 1]
                    name, response = await self._call_hedged(
                        name,
                        partner,
                        req,
                        deadline,
                        attempts,
                        partner_attempts,
                        hedge_delay,
                        skip,
                        partner_request=partner_req,
                    )
                    if name == partner:
                        req = partner_req
                else:
                    response = await self._call_provider(name, req, deadline, attempts)
            except HedgeFailure as hf:
                last_exc = hf.errors[hf.primary]
                for n, e in hf.errors.items():
                    failures.append((n, classify(e).kind))
                if any(
                    classify(e).kind is ErrorKind.DEADLINE for e in hf.errors.values()
                ):
                    raise last_exc
                info = classify(last_exc)
                if not (self.config.enable_failover and info.failover):
                    raise last_exc
                if not is_last:
                    self.failover_count += 1
                    tracing.add_event(
                        "failover",
                        {
                            "from": ",".join(hf.errors),
                            "to": candidates[index + 1],
                            "kind": info.kind.value,
                        },
                    )
                continue
            except Exception as exc:
                info = classify(exc)
                failures.append((name, info.kind))
                last_exc = exc
                if info.kind is ErrorKind.DEADLINE or not (
                    self.config.enable_failover and info.failover
                ):
                    raise
                if not is_last:
                    self.failover_count += 1
                    tracing.add_event(
                        "failover",
                        {
                            "from": name,
                            "to": candidates[index + 1],
                            "kind": info.kind.value,
                        },
                    )
                    logger.warning(
                        f"[{name}] failed ({info.kind.value}); "
                        f"failing over to {candidates[index + 1]}"
                    )
                continue

            await self._cache_response(name, req, response)
            if failures:
                response.metadata = {
                    **response.metadata,
                    "failed_over_from": [n for n, _ in failures],
                }
            if skip:
                response.metadata = {**response.metadata, "hedged": True}
            self._record_success(name, response.cost_usd, response.latency_ms)
            self._record_usage(
                name,
                response.model,
                response.latency_ms,
                response.input_tokens,
                response.output_tokens,
            )
            logger.bind(
                provider=name,
                model=response.model,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                cost_usd=response.cost_usd,
                latency_ms=round(response.latency_ms),
                failed_over_from=[n for n, _ in failures],
                hedged=bool(skip),
            ).info("provider response")
            return response

        if len(candidates) == 1 and last_exc is not None:
            raise last_exc
        raise AllProvidersUnavailable(
            failures, retry_after=self._soonest_retry_after()
        ) from last_exc

    async def stream(
        self,
        request: CompletionRequest,
        preferred_provider: Optional[str] = None,
        *,
        deadline_seconds: Optional[float] = None,
    ) -> AsyncGenerator[str, None]:
        """Stream a completion (see ``_stream``), inside a ``caips.stream`` span.

        The span is not made the current span: it must outlive the generator's
        yields, which can resume in a different context.
        """
        span = tracing.start_span(
            "caips.stream",
            {"caips.preferred_provider": preferred_provider},
            current=False,
        )
        inner = self._stream(
            request, preferred_provider, deadline_seconds=deadline_seconds, span=span
        )
        try:
            async for chunk in inner:
                yield chunk
        except (GeneratorExit, asyncio.CancelledError):
            raise  # the client left; not a failure
        except BaseException as exc:
            try:
                kind = classify(exc).kind.value
            except Exception:
                kind = None
            span.record_error(exc, kind)
            raise
        finally:
            # Close the inner stream now so it settles usage and releases its
            # rate-limit slot, rather than whenever it is garbage collected.
            await inner.aclose()
            usage = request.stream_usage
            if usage is not None and usage.provider:
                span.set_attributes(
                    {
                        "caips.provider": usage.provider,
                        "caips.model": usage.model,
                        "caips.input_tokens": usage.input_tokens,
                        "caips.output_tokens": usage.output_tokens,
                        "caips.cost_usd": usage.cost_usd,
                        "caips.usage_estimated": usage.estimated,
                        "caips.stream_completed": usage.completed,
                    }
                )
            span.end()

    async def _stream(
        self,
        request: CompletionRequest,
        preferred_provider: Optional[str] = None,
        *,
        deadline_seconds: Optional[float] = None,
        span: Any = None,
    ) -> AsyncGenerator[str, None]:
        """
        Stream a completion through the router.

        Opening the stream gets the same treatment as route(): breakers,
        rate limits, retries and failover. Failover and retries only happen
        *before* the first chunk is delivered. Once output has started, a
        failure is raised to the caller (the text already sent can't be
        recalled). The request deadline covers time to first chunk; after
        that each chunk must arrive within the provider's idle timeout.
        """
        span = span or tracing.current_span()
        self.request_count += 1
        self.resolve_alias(request)
        if preferred_provider is not None and preferred_provider not in self.providers:
            available = sorted(self.providers.keys())
            raise ValueError(
                f"Provider '{preferred_provider}' not available. "
                f"Available: {available}"
            )

        validate_conversation(request.messages)
        deadline = self._new_deadline(deadline_seconds)
        screened = self._screen(request, preferred_provider)
        candidates = [n for n, _ in screened]
        reqs = [r for _, r in screened]
        failures: list[tuple[str, ErrorKind]] = []
        last_exc: Optional[Exception] = None

        for index, name in enumerate(candidates):
            is_last = index == len(candidates) - 1
            req = reqs[index]
            breaker = self.breakers[name]
            if not breaker.allow():
                last_exc = CircuitOpenError(name, breaker.retry_after())
                failures.append((name, ErrorKind.CIRCUIT_OPEN))
                continue

            attempts = self.config.max_retry_attempts
            if not is_last:
                attempts = min(attempts, self.config.max_attempts_before_failover)

            timeout = self._timeout_for(name)
            started = False
            completed = False
            chars = 0  # text delivered, to estimate tokens if none are reported
            usage = StreamUsage()
            request.stream_usage = usage
            req.stream_usage = usage
            gen = None
            try:
                gen, first = await self._open_stream(
                    name, req, deadline, attempts, timeout, breaker
                )
            except Exception as exc:
                info = classify(exc)
                failures.append((name, info.kind))
                last_exc = exc
                if info.kind is ErrorKind.DEADLINE or not (
                    self.config.enable_failover and info.failover
                ):
                    raise
                if not is_last:
                    self.failover_count += 1
                    span.add_event(
                        "failover",
                        {
                            "from": name,
                            "to": candidates[index + 1],
                            "kind": info.kind.value,
                        },
                    )
                    logger.warning(
                        f"[{name}] stream failed to open ({info.kind.value}); "
                        f"failing over to {candidates[index + 1]}"
                    )
                continue

            # Stream is open and the first chunk (if any) is in hand.
            usage.opened_model = req.model or self.providers[name].default_model or ""
            try:
                if first is not _END:
                    started = True
                    chars += len(first)
                    yield first
                    while True:
                        try:
                            chunk = await asyncio.wait_for(
                                gen.__anext__(), timeout=timeout
                            )
                        except StopAsyncIteration:
                            break
                        except asyncio.TimeoutError:
                            raise ProviderTimeout(name, timeout)
                        chars += len(chunk)
                        yield chunk
            except Exception as exc:
                info = classify(exc)
                self._record_error(name, info.kind)
                if info.counts_against_breaker:
                    breaker.record_failure()
                else:
                    breaker.release()
                logger.error(
                    f"[{name}] stream interrupted ({info.kind.value}) "
                    f"after output started={started}"
                )
                raise
            except BaseException:
                # Client disconnected (GeneratorExit) or task cancelled: not a
                # provider failure, but the breaker's probe slot must be freed.
                breaker.release()
                raise
            else:
                completed = True
                breaker.record_success()
                self.provider_stats[name]["requests"] += 1
                return
            finally:
                self._settle_stream(name, req, usage, chars, started, completed)
                self.limiter.release(name)
                await self._close_stream(gen)

        if len(candidates) == 1 and last_exc is not None:
            raise last_exc
        raise AllProvidersUnavailable(
            failures, retry_after=self._soonest_retry_after()
        ) from last_exc

    # ------------------------------------------------------------------ #
    # Calling a provider (limits, timeout, retries, breaker accounting)
    # ------------------------------------------------------------------ #

    def _new_deadline(self, seconds: Optional[float]) -> Deadline:
        return Deadline(
            seconds if seconds is not None else self.config.deadline_seconds,
            clock=self._clock,
        )

    def _timeout_for(self, name: str) -> float:
        return self.config.provider_timeouts.get(
            name, self.config.request_timeout_seconds
        )

    async def _admit(self, name: str, request, deadline: Deadline) -> None:
        """Take a rate-limit slot, waiting no longer than the deadline allows.
        On failure the breaker probe slot taken by allow() is given back."""
        try:
            await self.limiter.acquire(
                name, self._estimate_tokens(request), max_wait=deadline.remaining()
            )
        except RateLimited:
            self._count_local_rate_limit(name)
            self.breakers[name].release()
            raise
        except BaseException:
            self.breakers[name].release()
            raise

    async def _call_provider(
        self,
        name: str,
        request: CompletionRequest,
        deadline: Deadline,
        attempts: int,
    ) -> CompletionResponse:
        """``_call_provider_untraced`` inside a ``caips.provider_call`` span."""
        with tracing.start_span(
            "caips.provider_call",
            {"caips.provider": name, "caips.max_attempts": attempts},
        ) as span:
            response = await self._call_provider_untraced(
                name, request, deadline, attempts
            )
            span.set_attributes(
                {
                    "caips.model": response.model,
                    "caips.input_tokens": response.input_tokens,
                    "caips.output_tokens": response.output_tokens,
                    "caips.cost_usd": response.cost_usd,
                }
            )
            return response

    async def _call_provider_untraced(
        self,
        name: str,
        request: CompletionRequest,
        deadline: Deadline,
        attempts: int,
    ) -> CompletionResponse:
        """Call one provider with a timeout, retries and breaker accounting.
        The caller must already have been admitted by breaker.allow()."""
        provider = self.providers[name]
        breaker = self.breakers[name]
        timeout = self._timeout_for(name)

        try:
            deadline.check()
        except DeadlineExceeded:
            breaker.release()
            raise
        await self._admit(name, request, deadline)

        async def one_call() -> CompletionResponse:
            seconds = deadline.clamp(timeout)
            if seconds is not None and seconds <= 0:
                raise DeadlineExceeded("request deadline exceeded")
            try:
                return await asyncio.wait_for(
                    provider.complete(request), timeout=seconds
                )
            except asyncio.TimeoutError:
                if deadline.expired():
                    raise DeadlineExceeded(f"deadline exceeded waiting on {name}")
                raise ProviderTimeout(name, seconds or timeout)

        try:
            if self.config.enable_retry and self.retry_manager:
                response = await self.retry_manager.execute(
                    one_call, deadline=deadline, max_attempts=attempts
                )
            else:
                response = await one_call()
        except asyncio.CancelledError:
            breaker.release()
            raise
        except Exception as exc:
            info = classify(exc)
            self._record_error(name, info.kind)
            if info.counts_against_breaker:
                breaker.record_failure()
            else:
                breaker.release()
            logger.error(
                f"[{name}] request failed ({info.kind.value}): {str(exc)[:120]}"
            )
            raise
        else:
            breaker.record_success()
            return response
        finally:
            self.limiter.release(name)

    async def _open_stream(
        self,
        name: str,
        request: CompletionRequest,
        deadline: Deadline,
        attempts: int,
        timeout: float,
        breaker: CircuitBreaker,
    ):
        """Open a provider stream and read its first chunk, with retries.
        Returns (async generator, first chunk or _END). On failure the
        breaker/limiter state is settled here and the error is raised."""
        provider = self.providers[name]
        try:
            deadline.check()
        except DeadlineExceeded:
            breaker.release()
            raise
        await self._admit(name, request, deadline)

        async def attempt():
            seconds = deadline.clamp(timeout)
            if seconds is not None and seconds <= 0:
                raise DeadlineExceeded("request deadline exceeded")
            gen = provider.stream(request)
            try:
                first = await asyncio.wait_for(gen.__anext__(), timeout=seconds)
            except StopAsyncIteration:
                return gen, _END
            except asyncio.TimeoutError:
                await self._close_stream(gen)
                if deadline.expired():
                    raise DeadlineExceeded(f"deadline exceeded waiting on {name}")
                raise ProviderTimeout(name, seconds or timeout)
            except BaseException:
                await self._close_stream(gen)
                raise
            return gen, first

        try:
            if self.config.enable_retry and self.retry_manager:
                return await self.retry_manager.execute(
                    attempt, deadline=deadline, max_attempts=attempts
                )
            return await attempt()
        except BaseException as exc:
            if isinstance(exc, Exception):
                info = classify(exc)
                self._record_error(name, info.kind)
                if info.counts_against_breaker:
                    breaker.record_failure()
                else:
                    breaker.release()
            else:
                breaker.release()
            self.limiter.release(name)
            raise

    @staticmethod
    async def _close_stream(gen) -> None:
        if gen is None:
            return
        try:
            await gen.aclose()
        except Exception:  # closing is best effort
            pass

    async def _cache_response(self, name: str, request, response) -> None:
        if self.cache and not request.stream and request.cacheable:
            messages_dict = [
                {"role": m.role, "content": m.content} for m in request.messages
            ]
            await self.cache.set(
                name,
                request.model or "*",
                messages_dict,
                dict(response.__dict__),
                ttl_seconds=self.config.cache_ttl_seconds,
                options=self._cache_options(request),
            )

    def alias_policy(self, model: Optional[str]) -> Optional[str]:
        """Policy behind a virtual model name, if it is one and usable."""
        target = self.aliases.get(model) if model else None
        if target is None or self.policies is None:
            return None
        return target if self.policies.current().get(target) is not None else None

    def resolve_alias(self, request: CompletionRequest) -> None:
        """Turn a virtual model (``fast``, ``cheap``, ``best``...) into a
        routing policy: the model is cleared and the policy named instead. A
        real catalogue model of the same name is never shadowed."""
        if self._model_registry.get(request.model or "") is not None:
            return
        policy = self.alias_policy(request.model)
        if policy is None:
            return
        request.metadata = {
            **(request.metadata or {}),
            "policy": policy,
            "alias": request.model,
        }
        request.model = None

    async def embed(
        self,
        texts: list[str],
        model: Optional[str] = None,
        preferred: Optional[str] = None,
    ):
        """Embed *texts* on the first healthy provider that supports it.
        Returns ``(vectors, input_tokens, model, provider)``."""
        names = [preferred] if preferred else list(self.providers)
        errors: list[tuple[str, ErrorKind]] = []
        last: Optional[Exception] = None
        for name in names:
            if name not in self.providers:
                raise ValueError(f"Provider '{name}' not available")
            p = self.providers[name]
            if not getattr(p, "supports_embeddings", False):
                continue
            if not self.breakers[name].allow():
                errors.append((name, ErrorKind.CIRCUIT_OPEN))
                continue
            try:
                vectors, tokens, used = await asyncio.wait_for(
                    p.embed(texts, model), timeout=self._timeout_for(name)
                )
            except Exception as exc:
                info = classify(exc)
                self._record_error(name, info.kind)
                if info.counts_against_breaker:
                    self.breakers[name].record_failure()
                else:
                    self.breakers[name].release()
                errors.append((name, info.kind))
                last = exc
                if not (self.config.enable_failover and info.failover):
                    raise
                continue
            self.breakers[name].record_success()
            self._record_usage(name, used, None, tokens, 0)
            return vectors, tokens, used, name
        if last is not None and len(errors) == 1:
            raise last
        if errors:
            raise AllProvidersUnavailable(
                errors, retry_after=self._soonest_retry_after()
            )
        raise ValueError("No configured provider supports embeddings")

    def _owner_of(self, model: Optional[str]) -> Optional[str]:
        """Provider that owns a catalogued model, or None if unknown."""
        profile = self._model_registry.get(model) if model else None
        if profile is not None:
            return profile.provider
        for name, p in self.providers.items():  # self-hosted servers list their models
            if model and getattr(p, "is_local", False) is True and model in p.models:
                return name
        return None

    def _candidates(
        self, request: CompletionRequest, preferred: Optional[str]
    ) -> list[str]:
        """Providers to try, best first. Pinned provider/model -> just one."""
        return self._plan(request, preferred)[0]

    def _plan(
        self, request: CompletionRequest, preferred: Optional[str]
    ) -> tuple[list[str], list[Optional[str]]]:
        """Candidates and models, with privacy applied: a request tagged
        ``private`` (``metadata.private``) is only ever sent to local servers."""
        order, models = self._plan_any(request, preferred)
        if not (request.metadata or {}).get("private"):
            return order, models
        kept = [
            (n, m)
            for n, m in zip(order, models)
            if getattr(self.providers[n], "is_local", False) is True
        ]
        if not kept:
            raise ValueError(
                "Request is private but no local provider can serve it; "
                "it was not sent to a cloud provider."
            )
        return [n for n, _ in kept], [m for _, m in kept]

    def _plan_any(
        self, request: CompletionRequest, preferred: Optional[str]
    ) -> tuple[list[str], list[Optional[str]]]:
        """Candidate providers and, in parallel, the model each should use
        (None = the provider's default)."""
        owner = self._owner_of(request.model)
        if preferred is not None:
            if owner is not None and owner != preferred:
                raise ValueError(
                    f"Model '{request.model}' belongs to provider '{owner}', "
                    f"not '{preferred}'"
                )
            return [preferred], [request.model]

        policy = self._policy_for(request)
        if request.model is not None and owner is not None:
            # An explicit model is served by the provider that owns it.
            if owner not in self.providers:
                raise ValueError(
                    f"Model '{request.model}' needs provider '{owner}', "
                    "which is not configured"
                )
            if policy is not None:
                reason = policy.permits(owner)
                if reason is not None:
                    raise ValueError(
                        f"Policy '{policy.name}' does not allow provider "
                        f"'{owner}' for model '{request.model}': {reason}"
                    )
            if not self.breakers[owner].would_allow():
                raise AllProvidersUnavailable(
                    [(owner, ErrorKind.CIRCUIT_OPEN)],
                    retry_after=self._soonest_retry_after(),
                )
            return [owner], [request.model]

        if policy is not None:
            order, _ = self._plan_with_policy(request, policy, advance=True)
            if request.model is not None:
                return order, [request.model] * len(order)
            return order, self._policy_models(policy, order)
        healthy = [n for n in self.providers if self.breakers[n].would_allow()]
        if not healthy:
            raise AllProvidersUnavailable(
                [(n, ErrorKind.CIRCUIT_OPEN) for n in self.providers],
                retry_after=self._soonest_retry_after(),
            )
        primary = self._select_provider(request, healthy)
        # An unrecognised model name can't be matched to a provider, so such
        # requests are not failed over.
        if not self.config.enable_failover or request.model is not None:
            return [primary], [request.model]
        rest = [n for n in healthy if n != primary]
        order = [primary] + self._failover_order(request, rest)
        return order, [None] * len(order)

    def _screen(
        self, request: CompletionRequest, preferred: Optional[str]
    ) -> list[tuple[str, CompletionRequest]]:
        """Plan candidates and drop those that cannot serve *request*.

        A pinned provider/model gets a hard error. In automatic routing a
        candidate that is over its model budget or incompatible with the
        request is skipped so the next one can serve it; only if none is left
        is the first reason raised."""
        names, models = self._plan(request, preferred)
        pinned = preferred is not None or request.model is not None
        keep: list[tuple[str, CompletionRequest]] = []
        problem: Optional[str] = None
        budget_exc: Optional[BudgetExceeded] = None
        for name, model in zip(names, models):
            req = (
                request
                if model == request.model
                else dataclasses.replace(request, model=model)
            )
            reason = self._model_problem(req, name)
            if reason:
                if pinned or len(names) == 1:
                    raise ValueError(reason)
                problem = problem or reason
                continue
            try:
                self._admit_model(req, name)
            except BudgetExceeded as exc:
                if pinned or len(names) == 1:
                    raise
                budget_exc = budget_exc or exc
                continue
            keep.append((name, req))
        if not keep:
            if budget_exc is not None:
                raise budget_exc
            raise ValueError(problem or "No provider can serve this request")
        return keep

    def _model_problem(self, req: CompletionRequest, name: str) -> Optional[str]:
        """Why the catalogued model can't serve *req*, or None. Unknown
        models are not blocked: the catalogue may lag behind new releases."""
        images = request_images(req)
        if images and not getattr(self.providers[name], "supports_vision", False):
            return f"Provider '{name}' does not accept image input."
        if req.tools:
            if req.stream:
                return "Tool calling is not supported with streaming yet."
            if not getattr(self.providers[name], "supports_tools", False):
                return f"Provider '{name}' does not support tool calling."
        hook = getattr(self.providers[name], "capability_problem", None)
        if hook is not None:
            problem = hook(req)
            if isinstance(problem, str):
                return problem
        profile = self._resolve_model_profile(name, req.model)
        if profile is None:
            return None
        if images and Modality.VISION not in profile.modalities:
            return f"{profile.model_id} does not accept image input."
        if req.tools and not profile.supports_function_calling:
            return f"{profile.model_id} does not support tool calling."
        estimated_input = self._estimate_tokens(req)
        out = req.max_tokens or 0
        if not profile.fits_context(estimated_input + out):
            return (
                f"Request (~{estimated_input} input + {out} max_tokens = "
                f"~{estimated_input + out} tokens) exceeds {profile.model_id}'s "
                f"{profile.context_window}-token context window."
            )
        if profile.max_output_tokens and out > profile.max_output_tokens:
            return (
                f"max_tokens={out} exceeds {profile.model_id}'s output limit "
                f"of {profile.max_output_tokens} tokens."
            )
        if req.stream and not profile.supports_streaming:
            return f"{profile.model_id} does not support streaming."
        return None

    def _policy_models(self, policy: Policy, order: list[str]) -> list[Optional[str]]:
        """Model per candidate: the policy's ``model`` for the first, its
        ``fallback_model`` for later ones, when the provider owns it."""
        models: list[Optional[str]] = []
        for i, name in enumerate(order):
            wanted = policy.model if i == 0 else policy.fallback_model
            owner = self._owner_of(wanted)
            models.append(wanted if wanted and owner in (None, name) else None)
        return models

    def _apply_policy_models(
        self, policy: Policy, order: list[str], eligible: list[str]
    ) -> list[str]:
        """Put the providers owning the policy's model / fallback_model first
        and second (a provider may repeat to retry with its fallback model)."""
        order = list(order)
        free = policy.strategy not in ("explicit", "fallback")
        if free and policy.model:
            o = self._owner_of(policy.model)
            if o in eligible:
                order = [o] + [n for n in order if n != o]
        if policy.fallback_model and self.config.enable_failover and order:
            o = self._owner_of(policy.fallback_model)
            if o in eligible and (o == order[0] or free):
                rest = [n for n in order[1:] if n != o]
                order = [order[0], o] + rest
        return order

    # ------------------------------------------------------------------ #
    # Declarative policies
    # ------------------------------------------------------------------ #

    def _policy_for(
        self, request: CompletionRequest, name: Optional[str] = None
    ) -> Optional[Policy]:
        """The policy for this request: ``name``, else ``metadata["policy"]``,
        else the policy called ``default``. None when no store is configured
        or no default exists. Naming an unknown policy is an error."""
        if self.policies is None:
            return None
        name = name or (request.metadata or {}).get("policy")
        policy_set = self.policies.current()
        policy = policy_set.get(name)
        if policy is None and name:
            raise ValueError(
                f"Unknown routing policy '{name}'. Available: {policy_set.names()}"
            )
        return policy

    def _plan_with_policy(
        self, request: CompletionRequest, policy: Policy, advance: bool = False
    ) -> tuple[list[str], list[dict]]:
        """Ordered providers to try under *policy*, plus a per-provider trace.

        Pure: no counters or round-robin state change, so explain() can call
        it freely. Raises AllProvidersUnavailable if every allowed provider
        has an open breaker, ValueError if the policy leaves none.
        """
        trace: dict[str, dict] = {n: {"provider": n} for n in self.providers}
        eligible: list[str] = []
        blocked: list[tuple[str, ErrorKind]] = []
        for name in self.providers:
            reason = policy.permits(name)
            if (
                reason is None
                and policy.strategy in ("fallback", "explicit")
                # An explicit policy without a chain pins its one provider.
                and (policy.fallback_chain or policy.strategy == "explicit")
            ):
                if name != policy.provider and name not in policy.fallback_chain:
                    reason = "not in policy fallback chain"
            if reason is not None:
                trace[name].update(status="excluded", reason=reason)
            elif not self.breakers[name].would_allow():
                trace[name].update(status="excluded", reason="circuit open")
                blocked.append((name, ErrorKind.CIRCUIT_OPEN))
            else:
                trace[name]["status"] = "eligible"
                eligible.append(name)

        if not eligible:
            if blocked:
                raise AllProvidersUnavailable(
                    blocked, retry_after=self._soonest_retry_after()
                )
            raise ValueError(f"Policy '{policy.name}' permits no registered provider")

        ranked = self._rank_for_policy(request, policy, eligible, trace)

        if policy.strategy == "explicit":
            primary = policy.provider if policy.provider in eligible else None
            if primary is None:
                trace.setdefault(policy.provider, {"provider": policy.provider})
                trace[policy.provider].setdefault("note", "pinned provider unavailable")
            ranked = ([primary] if primary else []) + [
                n for n in ranked if n != primary
            ]

        if policy.fallback_chain and policy.strategy != "fallback":
            chain = [n for n in policy.fallback_chain if n in eligible]
            head = ranked[:1]
            order = head + [n for n in chain if n not in head]
        else:
            order = ranked

        if advance and policy.strategy == "round_robin":
            self._policy_rr[policy.name] = self._policy_rr.get(policy.name, 0) + 1
        if request.model is None:
            order = self._apply_policy_models(policy, order, eligible)
        if request.model is not None or not self.config.enable_failover:
            order = order[:1]
        for pos, name in enumerate(order, 1):
            trace[name]["position"] = pos
        for name in eligible:
            if name not in order:
                trace[name][
                    "note"
                ] = "eligible but not used (failover disabled/pinned model)"
        return order, [trace[n] for n in self.providers]

    def _rank_for_policy(
        self,
        request: CompletionRequest,
        policy: Policy,
        eligible: list[str],
        trace: dict[str, dict],
    ) -> list[str]:
        strategy = policy.strategy
        if strategy == "fallback":
            return [n for n in policy.fallback_chain if n in eligible]
        if strategy == "round_robin":
            k = self._policy_rr.get(policy.name, 0) % len(eligible)
            return eligible[k:] + eligible[:k]
        if strategy == "fastest":
            lat = {n: self._p50_latency(n) or 500.0 for n in eligible}
            for n in eligible:
                trace[n]["p50_latency_ms"] = lat[n]
            ordered = sorted(eligible, key=lambda n: lat[n])
        else:  # balanced, cheapest, explicit (ranked behind the pinned provider)
            weights = (
                (1.0, 0.0)
                if strategy == "cheapest"
                else (
                    policy.cost_weight,
                    policy.latency_weight,
                )
            )
            scores = self._balanced_scores(request, eligible, weights)
            for n in eligible:
                trace[n]["score"] = round(scores[n], 6)
                trace[n]["health"] = round(self.health[n].score(), 4)
            ordered = sorted(eligible, key=lambda n: scores[n])

        ceiling = policy.latency_ceiling_ms
        if ceiling is not None:
            # Prefer providers within the latency ceiling; the rest stay
            # available as last-resort failover rather than being dropped.
            within = [n for n in ordered if (self._p50_latency(n) or 0.0) <= ceiling]
            over = [n for n in ordered if n not in within]
            for n in over:
                trace[n]["note"] = f"p50 above latency ceiling {ceiling:g}ms"
            ordered = within + over
        return ordered

    def explain(
        self,
        request: CompletionRequest,
        policy_name: Optional[str] = None,
        preferred_provider: Optional[str] = None,
    ) -> dict:
        """Dry run: say where *request* would go and why. Sends nothing and
        changes no routing state."""
        if preferred_provider is not None:
            if preferred_provider not in self.providers:
                raise ValueError(
                    f"Provider '{preferred_provider}' not available. "
                    f"Available: {sorted(self.providers)}"
                )
            return {
                "policy": None,
                "pinned_by_request": True,
                "order": [preferred_provider],
                "providers": [
                    {
                        "provider": n,
                        "status": "eligible" if n == preferred_provider else "excluded",
                        "reason": (
                            None
                            if n == preferred_provider
                            else "request pinned another provider"
                        ),
                    }
                    for n in self.providers
                ],
            }
        policy = self._policy_for(request, policy_name)
        if policy is None:
            healthy = [n for n in self.providers if self.breakers[n].would_allow()]
            if not healthy:
                raise AllProvidersUnavailable(
                    [(n, ErrorKind.CIRCUIT_OPEN) for n in self.providers],
                    retry_after=self._soonest_retry_after(),
                )
            order = [n for n in self._candidates_no_side_effects(request, healthy)]
            return {
                "policy": None,
                "strategy": self.config.strategy.value,
                "order": order,
                "providers": [
                    {
                        "provider": n,
                        "status": "eligible" if n in healthy else "excluded",
                        "reason": None if n in healthy else "circuit open",
                        **({"position": order.index(n) + 1} if n in order else {}),
                    }
                    for n in self.providers
                ],
            }
        order, trace = self._plan_with_policy(request, policy)
        return {
            "policy": policy.to_dict(),
            "order": order,
            "providers": trace,
        }

    def _candidates_no_side_effects(
        self, request: CompletionRequest, healthy: list[str]
    ) -> list[str]:
        """Candidate order without advancing round-robin / warm-up counters."""
        saved = self._rr_index
        try:
            primary = self._select_provider(request, healthy)
        finally:
            self._rr_index = saved
        if not self.config.enable_failover or request.model is not None:
            return [primary]
        rest = [n for n in healthy if n != primary]
        return [primary] + self._failover_order(request, rest)

    def _hedge_delay(
        self, request: CompletionRequest, preferred: Optional[str]
    ) -> Optional[float]:
        """Seconds to wait before hedging, or None when hedging is off."""
        if preferred is not None or request.stream or request.model is not None:
            return None
        policy = self._policy_for(request)
        if policy is not None and policy.hedge_after_ms is not None:
            return policy.hedge_after_ms / 1000.0
        return self.config.hedge_after_seconds

    async def _call_hedged(
        self,
        primary: str,
        partner: str,
        request: CompletionRequest,
        deadline: Deadline,
        attempts_primary: int,
        attempts_partner: int,
        delay: float,
        skip: set[str],
        partner_request: Optional[CompletionRequest] = None,
    ) -> tuple[str, CompletionResponse]:
        """Call *primary*; if it is still running after *delay* seconds, also
        call *partner* and return whichever succeeds first, cancelling the
        other. Returns (winner, response). If the primary fails before the
        delay, the exception propagates as for an ordinary call and the
        partner is left for normal failover. If both legs fail, raises
        HedgeFailure."""
        t1 = asyncio.ensure_future(
            self._call_provider(primary, request, deadline, attempts_primary)
        )
        done, _ = await asyncio.wait({t1}, timeout=delay)
        if t1 in done:
            return primary, t1.result()
        if not self.breakers[partner].allow():
            return primary, await t1

        t2 = asyncio.ensure_future(
            self._call_provider(
                partner, partner_request or request, deadline, attempts_partner
            )
        )
        skip.add(partner)
        self.hedge_count += 1
        names = {t1: primary, t2: partner}
        errors: dict[str, BaseException] = {}
        pending = {t1, t2}
        try:
            while pending:
                done, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED
                )
                for t in done:
                    exc = t.exception()
                    if exc is None:
                        for other in pending:
                            other.cancel()
                        await asyncio.gather(*pending, return_exceptions=True)
                        if names[t] == partner:
                            self.hedge_wins += 1
                        return names[t], t.result()
                    errors[names[t]] = exc
        except asyncio.CancelledError:
            for t in (t1, t2):
                t.cancel()
            await asyncio.gather(t1, t2, return_exceptions=True)
            raise
        raise HedgeFailure(errors, primary)

    def _failover_order(
        self, request: CompletionRequest, names: list[str]
    ) -> list[str]:
        """Order the remaining providers: registration order for FALLBACK,
        otherwise by the balanced cost/latency score (no side effects)."""
        if self.config.strategy == RoutingStrategy.FALLBACK or len(names) < 2:
            return list(names)
        scores = self._balanced_scores(request, names)
        return sorted(names, key=lambda n: scores[n])

    def _soonest_retry_after(self) -> Optional[float]:
        waits = [
            w for b in self.breakers.values() if (w := b.retry_after()) is not None
        ]
        return min(waits) if waits else None

    # ------------------------------------------------------------------ #
    # Provider selection
    # ------------------------------------------------------------------ #

    def _select_provider(
        self, request: CompletionRequest, available: Optional[list[str]] = None
    ) -> str:
        available = list(self.providers.keys()) if available is None else available
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
                estimated,
                self._estimate_output_tokens(request, estimated),
                request.model or p.default_model,
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

    def _balanced_scores(
        self,
        request: CompletionRequest,
        available: list[str],
        weights: Optional[tuple[float, float]] = None,
    ) -> dict[str, float]:
        """score = cost_weight * norm_cost + latency_weight * norm_latency
        (lower is better for both)."""
        estimated = self._estimate_tokens(request)

        raw_costs: dict[str, float] = {}
        raw_latencies: dict[str, float] = {}

        for name in available:
            p = self.providers[name]
            raw_costs[name] = p.estimate_cost(
                estimated,
                self._estimate_output_tokens(request, estimated),
                request.model or p.default_model,
            )
            history = self.provider_stats[name]["latency_ms_history"]
            raw_latencies[name] = (
                sorted(history)[len(history) // 2] if history else 500.0
            )

        # Normalise to [0, 1] — avoid division by zero
        max_cost = max(raw_costs.values()) or 1.0
        max_latency = max(raw_latencies.values()) or 1.0

        cost_w, latency_w = weights or (
            self.config.cost_weight,
            self.config.latency_weight,
        )
        return {
            name: (
                cost_w * raw_costs[name] / max_cost
                + latency_w * raw_latencies[name] / max_latency
                + self.config.health_weight * (1.0 - self.health[name].score())
            )
            for name in available
        }

    def _select_balanced(self, request: CompletionRequest, available: list[str]) -> str:
        """The provider with the lowest weighted cost + latency score wins."""
        scores = self._balanced_scores(request, available)
        best, best_score = available[0], float("inf")
        for name in available:
            if scores[name] < best_score:
                best_score = scores[name]
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

    def _resolve_model_profile(self, provider_name: str, model_id: Optional[str]):
        """
        Look up the ModelProfile for this request, preferring an explicit
        model_id and falling back to the provider's default. Returns None
        (rather than raising) if the catalogue doesn't recognise the model
        -- a newer model the catalogue hasn't been updated for yet should
        not block requests, it should just skip the context-window check.
        """
        try:
            if model_id:
                return self._model_registry.get(model_id)
            return self._model_registry.default_for(provider_name)
        except KeyError:
            return None

    def _settle_stream(
        self,
        name: str,
        request: CompletionRequest,
        usage: StreamUsage,
        chars: int,
        delivered: bool,
        completed: bool,
    ) -> None:
        """Price a stream once it has ended, however it ended.

        Output that reached the client was generated and billed even if the
        stream later failed or the client left, so it counts. Never raises:
        the stream's own outcome must not depend on accounting.
        """
        if not delivered:
            return
        try:
            provider = self.providers[name]
            usage.provider = name
            usage.completed = completed
            usage.model = usage.model or request.model or provider.default_model
            if not usage.reported:
                prompt_chars = sum(len(m.content) for m in request.messages)
                prompt_chars += len(request.system_prompt or "")
                usage.input_tokens = max(1, prompt_chars // 4)
                usage.output_tokens = max(1, chars // 4)
                usage.estimated = True
                self.streams_estimated[name] = self.streams_estimated.get(name, 0) + 1
            usage.cost_usd = provider._bill(
                usage.input_tokens, usage.output_tokens, usage.model
            )
            self.provider_stats[name]["total_cost"] += usage.cost_usd
            # No latency: a long generation is not a slow provider.
            self._record_usage(
                name, usage.model, None, usage.input_tokens, usage.output_tokens
            )
            logger.bind(
                provider=name,
                model=usage.model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cost_usd=usage.cost_usd,
                estimated=usage.estimated,
                completed=completed,
            ).log("INFO" if completed else "WARNING", "stream settled")
        except Exception:
            logger.exception(f"[{name}] could not settle stream usage")

    def _admit_model(self, request, name: str) -> None:
        """Enforce per-model caps on the model this candidate will use. This
        covers requests that omit ``model`` (auto routing) and failover."""
        if self.model_gate is not None:
            self.model_gate(request.model or self.providers[name].default_model)

    @staticmethod
    def _cache_options(request: CompletionRequest) -> dict:
        return {
            "system_prompt": request.system_prompt,
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
            "response_format": request.response_format,
        }

    @staticmethod
    def _estimate_output_tokens(request: CompletionRequest, fallback: int) -> int:
        """Output size for cost ranking: the requested limit when set."""
        return request.max_tokens if request.max_tokens else fallback

    def _estimate_tokens(self, request: CompletionRequest) -> int:
        total_chars = sum(len(m.content) for m in request.messages)
        total_chars += len(request.system_prompt or "")
        total_chars += _IMAGE_CHARS * len(request_images(request))
        return max(100, total_chars // 4)

    def _model_entry(self, name: str, model: str) -> dict:
        key = (name, model)
        entry = self.model_stats.get(key)
        if entry is None:
            # Bound label cardinality: past the cap, extra models share "other".
            known = sum(1 for p, _ in self.model_stats if p == name)
            if known >= _MAX_MODELS_PER_PROVIDER:
                key = (name, "other")
                entry = self.model_stats.get(key)
            if entry is None:
                entry = {
                    "requests": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "latency_sum_ms": 0.0,
                    "latency_buckets": [0] * (len(LATENCY_BUCKETS_MS) + 1),
                }
                self.model_stats[key] = entry
        return entry

    def _record_usage(
        self,
        name: str,
        model: str,
        latency_ms: Optional[float],
        in_tok: int,
        out_tok: int,
    ) -> None:
        entry = self._model_entry(name, model or "unknown")
        entry["requests"] += 1
        entry["input_tokens"] += max(0, in_tok or 0)
        entry["output_tokens"] += max(0, out_tok or 0)
        if latency_ms is None:  # streams: tokens count, latency does not
            return
        entry["latency_sum_ms"] += latency_ms
        for i, bound in enumerate(LATENCY_BUCKETS_MS):
            if latency_ms <= bound:
                entry["latency_buckets"][i] += 1
                return
        entry["latency_buckets"][-1] += 1  # +Inf

    def _record_success(self, name: str, cost_usd: float, latency_ms: float):
        stats = self.provider_stats[name]
        stats["requests"] += 1
        self.health[name].record(True)
        stats["total_cost"] += cost_usd
        history = stats["latency_ms_history"]
        history.append(latency_ms)
        if len(history) > _LATENCY_HISTORY_SIZE:
            history.pop(0)

    def _record_error(self, name: str, kind: Optional[ErrorKind] = None):
        stats = self.provider_stats[name]
        stats["errors"] += 1
        if kind is not ErrorKind.BAD_REQUEST:  # the caller's fault, not the provider's
            self.health[name].record(False)
        key = (kind or ErrorKind.UNKNOWN).value
        stats["error_kinds"][key] = stats["error_kinds"].get(key, 0) + 1

    def _count_local_rate_limit(self, name: str):
        """Our own limiter turned a request away (not a provider failure)."""
        kinds = self.provider_stats[name]["error_kinds"]
        kinds["local_rate_limit"] = kinds.get("local_rate_limit", 0) + 1

    def _p50_latency(self, name: str) -> Optional[float]:
        history = self.provider_stats[name]["latency_ms_history"]
        if not history:
            return None
        return sorted(history)[len(history) // 2]

    def stats(self) -> dict:
        return {
            "total_requests": self.request_count,
            "failovers": self.failover_count,
            "hedges": {"launched": self.hedge_count, "won_by_second": self.hedge_wins},
            "strategy": self.config.strategy,
            "provider_stats": {
                name: {
                    "requests": s["requests"],
                    "errors": s["errors"],
                    "total_cost": s["total_cost"],
                    "p50_latency_ms": self._p50_latency(name),
                    "health": round(self.health[name].score(), 4),
                    "error_kinds": dict(s["error_kinds"]),
                    "circuit": self.breakers[name].snapshot(),
                }
                for name, s in self.provider_stats.items()
            },
            "model_stats": [
                {
                    "provider": p,
                    "model": m,
                    **e,
                    "latency_buckets": list(e["latency_buckets"]),
                }
                for (p, m), e in self.model_stats.items()
            ],
            "latency_bucket_bounds_ms": list(LATENCY_BUCKETS_MS),
            "unpriced_models": get_pricing().unpriced_stats(),
            "streams_estimated": dict(self.streams_estimated),
            "cache_stats": self.cache.stats() if self.cache else None,
            "retry_stats": self.retry_manager.stats() if self.retry_manager else None,
            "limiter_stats": self.limiter.stats(),
        }
