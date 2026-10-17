import asyncio
import os
import time
from enum import Enum
from typing import AsyncGenerator, Callable, Optional

from loguru import logger

from neural.model import get_registry
from providers import BaseProvider, CompletionRequest, CompletionResponse

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
from .limiter import GlobalRateLimiter, RateLimitConfig
from .policy import Policy, PolicyStore
from .retry import RetryConfig, RetryManager

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

# Per-attempt timeout for one provider call, and per-provider overrides.
# Slow providers (reasoning models) get longer; the request deadline still caps
# the total time across retries and failovers.
_DEFAULT_REQUEST_TIMEOUT_S = 60.0
_DEFAULT_PROVIDER_TIMEOUTS = {"deepseek": 120.0}
_DEFAULT_DEADLINE_S = 120.0


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
            self.limiter.register_provider(name, RateLimitConfig())
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

        # Routing state
        self.request_count = 0
        self._rr_index = 0  # only used by round-robin
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
        """
        Route a request to the best available provider.

        Providers with an open circuit breaker are skipped. If the chosen
        provider fails with a provider-side error (overload, 5xx, timeout,
        connection, auth, quota), the router fails over to the next-best
        provider, unless the caller pinned a provider or a specific model.
        All attempts share one deadline.
        """
        self.request_count += 1

        # Validate explicit provider choice up front so the error is clear.
        if preferred_provider is not None and preferred_provider not in self.providers:
            available = sorted(self.providers.keys())
            raise ValueError(
                f"Provider '{preferred_provider}' not available. "
                f"Available: {available}"
            )

        deadline = self._new_deadline(deadline_seconds)
        candidates = self._candidates(request, preferred_provider)
        provider_name = candidates[0]

        # Fail fast on context-window overflow using the neural model
        # registry, instead of letting the provider API reject it after
        # we've already spent a rate-limit slot and a cache lookup on it.
        profile = self._resolve_model_profile(provider_name, request.model)
        if profile is not None:
            estimated_input = self._estimate_tokens(request)
            headroom = estimated_input + (request.max_tokens or 0)
            if not profile.fits_context(headroom):
                raise ValueError(
                    f"Request (~{estimated_input} input + "
                    f"{request.max_tokens or 0} max_tokens = ~{headroom} tokens) "
                    f"exceeds {profile.model_id}'s {profile.context_window}-token "
                    f"context window."
                )

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

        failures: list[tuple[str, ErrorKind]] = []
        last_exc: Optional[Exception] = None

        for index, name in enumerate(candidates):
            is_last = index == len(candidates) - 1
            if not self.breakers[name].allow():
                last_exc = CircuitOpenError(name, self.breakers[name].retry_after())
                failures.append((name, ErrorKind.CIRCUIT_OPEN))
                continue

            attempts = self.config.max_retry_attempts
            if not is_last:
                attempts = min(attempts, self.config.max_attempts_before_failover)

            try:
                response = await self._call_provider(name, request, deadline, attempts)
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
                    logger.warning(
                        f"[{name}] failed ({info.kind.value}); "
                        f"failing over to {candidates[index + 1]}"
                    )
                continue

            await self._cache_response(name, request, response)
            if failures:
                response.metadata = {
                    **response.metadata,
                    "failed_over_from": [n for n, _ in failures],
                }
            self._record_success(name, response.cost_usd, response.latency_ms)
            logger.info(
                f"[{name}] {response.model} | "
                f"{response.input_tokens}in {response.output_tokens}out | "
                f"${response.cost_usd:.6f} | {response.latency_ms:.0f}ms"
            )
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
        """
        Stream a completion through the router.

        Opening the stream gets the same treatment as route(): breakers,
        rate limits, retries and failover. Failover and retries only happen
        *before* the first chunk is delivered. Once output has started, a
        failure is raised to the caller (the text already sent can't be
        recalled). The request deadline covers time to first chunk; after
        that each chunk must arrive within the provider's idle timeout.
        """
        self.request_count += 1
        if preferred_provider is not None and preferred_provider not in self.providers:
            available = sorted(self.providers.keys())
            raise ValueError(
                f"Provider '{preferred_provider}' not available. "
                f"Available: {available}"
            )

        deadline = self._new_deadline(deadline_seconds)
        candidates = self._candidates(request, preferred_provider)
        failures: list[tuple[str, ErrorKind]] = []
        last_exc: Optional[Exception] = None

        for index, name in enumerate(candidates):
            is_last = index == len(candidates) - 1
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
            gen = None
            try:
                gen, first = await self._open_stream(
                    name, request, deadline, attempts, timeout, breaker
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
                    logger.warning(
                        f"[{name}] stream failed to open ({info.kind.value}); "
                        f"failing over to {candidates[index + 1]}"
                    )
                continue

            # Stream is open and the first chunk (if any) is in hand.
            try:
                if first is not _END:
                    started = True
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
                breaker.record_success()
                self.provider_stats[name]["requests"] += 1
                return
            finally:
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
        if self.cache and not request.stream:
            messages_dict = [
                {"role": m.role, "content": m.content} for m in request.messages
            ]
            await self.cache.set(
                name,
                request.model or "*",
                messages_dict,
                dict(response.__dict__),
                ttl_seconds=self.config.cache_ttl_seconds,
            )

    def _candidates(
        self, request: CompletionRequest, preferred: Optional[str]
    ) -> list[str]:
        """Providers to try, best first. Pinned provider/model -> just one."""
        if preferred is not None:
            return [preferred]
        policy = self._policy_for(request)
        if policy is not None:
            order, _ = self._plan_with_policy(request, policy)
            return order
        healthy = [n for n in self.providers if self.breakers[n].would_allow()]
        if not healthy:
            raise AllProvidersUnavailable(
                [(n, ErrorKind.CIRCUIT_OPEN) for n in self.providers],
                retry_after=self._soonest_retry_after(),
            )
        primary = self._select_provider(request, healthy)
        # A request for a specific model can't be served by a different
        # provider, so only model-agnostic requests fail over.
        if not self.config.enable_failover or request.model is not None:
            return [primary]
        rest = [n for n in healthy if n != primary]
        return [primary] + self._failover_order(request, rest)

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
        self, request: CompletionRequest, policy: Policy
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
                and policy.fallback_chain
                and policy.strategy
                in (
                    "fallback",
                    "explicit",
                )
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
            return list(eligible)
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
                estimated, estimated, request.model or p.default_model
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

    def _record_error(self, name: str, kind: Optional[ErrorKind] = None):
        stats = self.provider_stats[name]
        stats["errors"] += 1
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
            "strategy": self.config.strategy,
            "provider_stats": {
                name: {
                    "requests": s["requests"],
                    "errors": s["errors"],
                    "total_cost": s["total_cost"],
                    "p50_latency_ms": self._p50_latency(name),
                    "error_kinds": dict(s["error_kinds"]),
                    "circuit": self.breakers[name].snapshot(),
                }
                for name, s in self.provider_stats.items()
            },
            "cache_stats": self.cache.stats() if self.cache else None,
            "retry_stats": self.retry_manager.stats() if self.retry_manager else None,
            "limiter_stats": self.limiter.stats(),
        }
