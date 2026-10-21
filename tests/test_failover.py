"""
Router reliability: failover, circuit breaking, timeouts, deadlines, streaming.
All providers are fakes; time is controlled where it matters.
"""

import asyncio

import pytest

from coreai.circuit_breaker import CircuitState
from coreai.errors import (
    AllProvidersUnavailable,
    CircuitOpenError,
    DeadlineExceeded,
    ErrorKind,
    ProviderTimeout,
)
from coreai.router import RoutingStrategy
from tests.helpers import (
    FakeClock,
    StatusError,
    hang_forever,
    make_provider,
    make_router,
    request,
)

# --------------------------------------------------------------------------
# Failover
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        StatusError(529, "Overloaded"),  # Anthropic overload
        StatusError(500),
        StatusError(503),
        StatusError(429),
        TimeoutError("slow"),
        ConnectionError("reset"),
        StatusError(401, "bad key"),  # this provider's key is wrong, another works
    ],
)
async def test_fails_over_to_next_provider_on_provider_errors(error):
    a = make_provider("a", behaviour=error)
    b = make_provider("b")
    router = make_router({"a": a, "b": b}, max_retry_attempts=1)

    resp = await router.route(request())

    assert resp.provider == "b"
    assert resp.metadata["failed_over_from"] == ["a"]
    assert router.stats()["failovers"] == 1
    assert router.provider_stats["a"]["errors"] == 1


@pytest.mark.asyncio
async def test_bad_request_does_not_fail_over():
    a = make_provider("a", behaviour=StatusError(400, "bad param"))
    b = make_provider("b")
    router = make_router({"a": a, "b": b})
    with pytest.raises(StatusError):
        await router.route(request())
    b.complete.assert_not_called()


@pytest.mark.asyncio
async def test_unknown_errors_do_not_fail_over():
    a = make_provider("a", behaviour=RuntimeError("our bug"))
    b = make_provider("b")
    router = make_router({"a": a, "b": b})
    with pytest.raises(RuntimeError):
        await router.route(request())
    b.complete.assert_not_called()


@pytest.mark.asyncio
async def test_all_providers_failing_raises_with_every_reason():
    a = make_provider("a", behaviour=StatusError(529))
    b = make_provider("b", behaviour=StatusError(500))
    router = make_router({"a": a, "b": b}, max_retry_attempts=1)

    with pytest.raises(AllProvidersUnavailable) as info:
        await router.route(request())

    assert info.value.failures == [
        ("a", ErrorKind.OVERLOADED),
        ("b", ErrorKind.SERVER_ERROR),
    ]


@pytest.mark.asyncio
async def test_single_provider_failure_raises_the_original_error():
    a = make_provider("a", behaviour=StatusError(500, "boom"))
    router = make_router({"a": a}, max_retry_attempts=1)
    with pytest.raises(StatusError, match="boom"):
        await router.route(request())


@pytest.mark.asyncio
async def test_pinned_provider_never_fails_over():
    a = make_provider("a", behaviour=StatusError(500))
    b = make_provider("b")
    router = make_router({"a": a, "b": b}, max_retry_attempts=1)
    with pytest.raises(StatusError):
        await router.route(request(), preferred_provider="a")
    b.complete.assert_not_called()


@pytest.mark.asyncio
async def test_request_for_a_specific_model_never_fails_over():
    """A model name belongs to one provider; another can't serve it."""
    a = make_provider("a", behaviour=StatusError(500))
    b = make_provider("b")
    router = make_router({"a": a, "b": b}, max_retry_attempts=1)
    with pytest.raises(StatusError):
        await router.route(request(model="a-special-model"))
    b.complete.assert_not_called()


@pytest.mark.asyncio
async def test_failover_can_be_disabled():
    a = make_provider("a", behaviour=StatusError(500))
    b = make_provider("b")
    router = make_router({"a": a, "b": b}, enable_failover=False, max_retry_attempts=1)
    with pytest.raises(StatusError):
        await router.route(request())
    b.complete.assert_not_called()


@pytest.mark.asyncio
async def test_fallback_strategy_really_falls_back_in_registration_order():
    a = make_provider("a", behaviour=StatusError(529))
    b = make_provider("b", behaviour=StatusError(529))
    c = make_provider("c")
    router = make_router(
        {"a": a, "b": b, "c": c},
        strategy=RoutingStrategy.FALLBACK,
        max_retry_attempts=1,
    )
    resp = await router.route(request())
    assert resp.provider == "c"
    assert resp.metadata["failed_over_from"] == ["a", "b"]


@pytest.mark.asyncio
async def test_cheapest_strategy_fails_over_to_next_cheapest():
    a = make_provider("a", cost=0.001, behaviour=StatusError(500))
    b = make_provider("b", cost=0.005)
    c = make_provider("c", cost=0.003)
    router = make_router(
        {"a": a, "b": b, "c": c},
        strategy=RoutingStrategy.CHEAPEST,
        max_retry_attempts=1,
    )
    resp = await router.route(request())
    # a is cheapest and fails; the balanced ranking of the rest picks c over b.
    assert resp.provider == "c"


@pytest.mark.asyncio
async def test_failed_over_response_is_cached_under_the_serving_provider():
    a = make_provider("a", behaviour=StatusError(500))
    b = make_provider("b")
    router = make_router({"a": a, "b": b}, enable_cache=True, max_retry_attempts=1)
    await router.route(request())
    cached = await router.cache.get(
        "b",
        "*",
        [{"role": "user", "content": "hello"}],
        router._cache_options(request()),
    )
    assert cached is not None
    assert "failed_over_from" not in cached["metadata"]  # cache copy is unaffected


# --------------------------------------------------------------------------
# Retry inside a provider before failing over
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retries_the_same_provider_before_failing_over():
    a = make_provider("a", behaviour=[StatusError(529), None])  # fails once, then ok
    b = make_provider("b")
    router = make_router({"a": a, "b": b}, enable_retry=True)
    resp = await router.route(request())
    assert resp.provider == "a"
    assert a.complete.await_count == 2
    b.complete.assert_not_called()
    assert router.stats()["retry_stats"]["retries_by_kind"] == {"overloaded": 1}


@pytest.mark.asyncio
async def test_limits_attempts_on_a_failing_provider_when_alternatives_exist():
    a = make_provider("a", behaviour=StatusError(529))
    b = make_provider("b")
    router = make_router(
        {"a": a, "b": b},
        enable_retry=True,
        max_retry_attempts=5,
        max_attempts_before_failover=2,
    )
    resp = await router.route(request())
    assert resp.provider == "b"
    assert a.complete.await_count == 2  # not 5


@pytest.mark.asyncio
async def test_last_provider_gets_the_full_retry_budget():
    a = make_provider("a", behaviour=StatusError(529))
    router = make_router({"a": a}, enable_retry=True, max_retry_attempts=4)
    with pytest.raises(StatusError):
        await router.route(request())
    assert a.complete.await_count == 4


# --------------------------------------------------------------------------
# Circuit breaker integration
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_breaker_opens_and_the_failing_provider_is_skipped():
    clock = FakeClock()
    a = make_provider("a", behaviour=StatusError(529))
    b = make_provider("b")
    router = make_router(
        {"a": a, "b": b},
        clock=clock,
        circuit_failure_threshold=3,
        max_retry_attempts=1,
    )

    for _ in range(3):
        resp = await router.route(request())
        assert resp.provider == "b"
    assert router.breakers["a"].state is CircuitState.OPEN

    calls_before = a.complete.await_count
    resp = await router.route(request())
    assert resp.provider == "b"
    assert a.complete.await_count == calls_before  # a was not even tried


@pytest.mark.asyncio
async def test_provider_recovers_through_a_half_open_probe():
    clock = FakeClock()
    a = make_provider("a", behaviour=[StatusError(529)] * 3 + [None])
    b = make_provider("b")
    router = make_router(
        {"a": a, "b": b},
        clock=clock,
        circuit_failure_threshold=3,
        circuit_reset_seconds=30,
        max_retry_attempts=1,
    )
    for _ in range(3):
        await router.route(request())
    assert router.breakers["a"].state is CircuitState.OPEN

    clock.advance(31)  # reset timeout passes: next request probes a
    resp = await router.route(request())
    assert resp.provider == "a"
    assert router.breakers["a"].state is CircuitState.CLOSED


@pytest.mark.asyncio
async def test_failed_probe_reopens_the_circuit():
    clock = FakeClock()
    a = make_provider("a", behaviour=StatusError(529))
    b = make_provider("b")
    router = make_router(
        {"a": a, "b": b},
        clock=clock,
        circuit_failure_threshold=2,
        circuit_reset_seconds=30,
        max_retry_attempts=1,
    )
    for _ in range(2):
        await router.route(request())
    clock.advance(31)
    resp = await router.route(request())  # probe a fails, falls over to b
    assert resp.provider == "b"
    assert router.breakers["a"].state is CircuitState.OPEN


@pytest.mark.asyncio
async def test_bad_requests_do_not_open_the_breaker():
    a = make_provider("a", behaviour=StatusError(400))
    router = make_router({"a": a}, circuit_failure_threshold=2)
    for _ in range(5):
        with pytest.raises(StatusError):
            await router.route(request())
    assert router.breakers["a"].state is CircuitState.CLOSED


@pytest.mark.asyncio
async def test_all_breakers_open_fails_fast_with_retry_after():
    clock = FakeClock()
    a = make_provider("a", behaviour=StatusError(500))
    router = make_router(
        {"a": a},
        clock=clock,
        circuit_failure_threshold=1,
        circuit_reset_seconds=30,
        max_retry_attempts=1,
    )
    with pytest.raises(StatusError):
        await router.route(request())
    calls = a.complete.await_count

    clock.advance(10)
    with pytest.raises(AllProvidersUnavailable) as info:
        await router.route(request())
    assert info.value.retry_after == pytest.approx(20.0)
    assert a.complete.await_count == calls  # no call was made


@pytest.mark.asyncio
async def test_pinned_provider_with_open_breaker_raises_circuit_open():
    a = make_provider("a", behaviour=StatusError(500))
    b = make_provider("b")
    router = make_router(
        {"a": a, "b": b}, circuit_failure_threshold=1, max_retry_attempts=1
    )
    with pytest.raises(StatusError):
        await router.route(request(), preferred_provider="a")
    with pytest.raises(CircuitOpenError):
        await router.route(request(), preferred_provider="a")


# --------------------------------------------------------------------------
# Timeouts and deadlines
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hung_provider_times_out_and_fails_over():
    a = make_provider("a", behaviour=hang_forever())
    b = make_provider("b")
    router = make_router(
        {"a": a, "b": b}, request_timeout_seconds=0.05, max_retry_attempts=1
    )
    resp = await router.route(request())
    assert resp.provider == "b"
    assert router.provider_stats["a"]["error_kinds"] == {"timeout": 1}


@pytest.mark.asyncio
async def test_per_provider_timeout_overrides_the_default():
    a = make_provider("a", behaviour=hang_forever())
    router = make_router(
        {"a": a},
        request_timeout_seconds=30,
        provider_timeouts={"a": 0.05},
        max_retry_attempts=1,
    )
    with pytest.raises(ProviderTimeout):
        await router.route(request())


@pytest.mark.asyncio
async def test_deadline_caps_total_time_across_failovers():
    a = make_provider("a", behaviour=hang_forever())
    b = make_provider("b", behaviour=hang_forever())
    router = make_router(
        {"a": a, "b": b},
        request_timeout_seconds=10,
        deadline_seconds=0.15,
        max_retry_attempts=1,
    )
    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(DeadlineExceeded):
        await router.route(request())
    assert loop.time() - started < 1.0  # not 20s


@pytest.mark.asyncio
async def test_per_request_deadline_override():
    a = make_provider("a", behaviour=hang_forever())
    router = make_router({"a": a}, request_timeout_seconds=10, deadline_seconds=60)
    with pytest.raises(DeadlineExceeded):
        await router.route(request(), deadline_seconds=0.05)


@pytest.mark.asyncio
async def test_deadline_exceeded_does_not_count_against_the_breaker():
    a = make_provider("a", behaviour=hang_forever())
    router = make_router(
        {"a": a},
        request_timeout_seconds=10,
        deadline_seconds=0.05,
        circuit_failure_threshold=1,
    )
    with pytest.raises(DeadlineExceeded):
        await router.route(request())
    assert router.breakers["a"].state is CircuitState.CLOSED


@pytest.mark.asyncio
async def test_cancelling_a_request_releases_its_resources():
    a = make_provider("a", behaviour=hang_forever())
    router = make_router({"a": a}, request_timeout_seconds=10, deadline_seconds=60)
    task = asyncio.create_task(router.route(request()))
    await asyncio.sleep(0.05)
    assert router.limiter.get_limiter("a").active_requests == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert router.limiter.get_limiter("a").active_requests == 0
    assert router.breakers["a"].state is CircuitState.CLOSED


@pytest.mark.asyncio
async def test_slots_are_released_after_failures():
    a = make_provider("a", behaviour=StatusError(500))
    b = make_provider("b")
    router = make_router({"a": a, "b": b}, max_retry_attempts=1)
    for _ in range(20):
        await router.route(request())
    assert router.limiter.get_limiter("a").active_requests == 0
    assert router.limiter.get_limiter("b").active_requests == 0


# --------------------------------------------------------------------------
# Stats
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stats_expose_error_kinds_failovers_and_breaker_state():
    a = make_provider("a", behaviour=StatusError(529))
    b = make_provider("b")
    router = make_router({"a": a, "b": b}, max_retry_attempts=1)
    await router.route(request())
    stats = router.stats()
    assert stats["failovers"] == 1
    assert stats["provider_stats"]["a"]["error_kinds"] == {"overloaded": 1}
    assert stats["provider_stats"]["a"]["circuit"]["state"] == "closed"


@pytest.mark.asyncio
async def test_cancelled_half_open_probe_frees_the_probe_slot():
    """If the one allowed probe is cancelled (client left), the breaker must not
    stay wedged half-open with a phantom in-flight probe."""
    clock = FakeClock()
    a = make_provider("a", behaviour=[StatusError(500)] + [hang_forever()])
    router = make_router(
        {"a": a},
        clock=clock,
        circuit_failure_threshold=1,
        circuit_reset_seconds=30,
        request_timeout_seconds=10,
        deadline_seconds=60,
        max_retry_attempts=1,
    )
    with pytest.raises(StatusError):
        await router.route(request())
    clock.advance(31)

    a.complete.side_effect = hang_forever()
    task = asyncio.create_task(router.route(request()))
    await asyncio.sleep(0.05)  # probe in flight
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert router.breakers["a"].would_allow()  # slot is free again


# --------------------------------------------------------------------------
# Streaming through the router
# --------------------------------------------------------------------------

from tests.helpers import collect, make_stream_provider  # noqa: E402


@pytest.mark.asyncio
async def test_stream_yields_all_chunks():
    a = make_stream_provider("a", chunks=("x", "y", "z"))
    router = make_router({"a": a})
    assert await collect(router.stream(request())) == ["x", "y", "z"]
    assert router.provider_stats["a"]["requests"] == 1
    assert router.limiter.get_limiter("a").active_requests == 0


@pytest.mark.asyncio
async def test_stream_fails_over_before_the_first_chunk():
    a = make_stream_provider("a", open_error=StatusError(529))
    b = make_stream_provider("b", chunks=("from-b",))
    router = make_router({"a": a, "b": b}, max_retry_attempts=1)
    assert await collect(router.stream(request())) == ["from-b"]
    assert router.stats()["failovers"] == 1


@pytest.mark.asyncio
async def test_stream_retries_opening_before_failing_over():
    calls = {"n": 0}
    good = make_stream_provider("a", chunks=("ok",))
    real = good.stream.side_effect

    async def flaky_open(req):
        calls["n"] += 1
        if calls["n"] == 1:
            raise StatusError(529)
        async for c in real(req):
            yield c

    good.stream = lambda req: flaky_open(req)
    router = make_router({"a": good}, enable_retry=True, max_retry_attempts=3)
    assert await collect(router.stream(request())) == ["ok"]
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_stream_failure_after_output_started_is_raised_not_failed_over():
    a = make_stream_provider(
        "a", chunks=("one", "two", "three"), fail_after=(1, StatusError(500))
    )
    b = make_stream_provider("b", chunks=("other",))
    router = make_router({"a": a, "b": b}, max_retry_attempts=1)

    got = []
    with pytest.raises(StatusError):
        async for chunk in router.stream(request()):
            got.append(chunk)

    assert got == ["one"]  # partial output delivered, then the error surfaced
    b.stream.assert_not_called()  # can't splice another provider's answer in
    assert router.provider_stats["a"]["error_kinds"] == {"server_error": 1}
    assert router.limiter.get_limiter("a").active_requests == 0


@pytest.mark.asyncio
async def test_stream_hung_before_first_chunk_times_out_and_fails_over():
    a = make_stream_provider("a", hang_first=True)
    b = make_stream_provider("b", chunks=("b",))
    router = make_router(
        {"a": a, "b": b}, request_timeout_seconds=0.05, max_retry_attempts=1
    )
    assert await collect(router.stream(request())) == ["b"]


@pytest.mark.asyncio
async def test_stream_idle_timeout_after_output_started():
    a = make_stream_provider("a", chunks=("one", "two"), hang_after=1)
    router = make_router({"a": a}, request_timeout_seconds=0.05)
    got = []
    with pytest.raises(ProviderTimeout):
        async for chunk in router.stream(request()):
            got.append(chunk)
    assert got == ["one"]


@pytest.mark.asyncio
async def test_stream_bad_request_does_not_fail_over():
    a = make_stream_provider("a", open_error=StatusError(400))
    b = make_stream_provider("b")
    router = make_router({"a": a, "b": b})
    with pytest.raises(StatusError):
        await collect(router.stream(request()))
    b.stream.assert_not_called()


@pytest.mark.asyncio
async def test_stream_all_providers_down():
    a = make_stream_provider("a", open_error=StatusError(529))
    b = make_stream_provider("b", open_error=StatusError(503))
    router = make_router({"a": a, "b": b}, max_retry_attempts=1)
    with pytest.raises(AllProvidersUnavailable):
        await collect(router.stream(request()))


@pytest.mark.asyncio
async def test_stream_unknown_provider_is_a_clear_error():
    router = make_router({"a": make_stream_provider("a")})
    with pytest.raises(ValueError, match="not available"):
        await collect(router.stream(request(), preferred_provider="nope"))


@pytest.mark.asyncio
async def test_stream_client_disconnect_releases_slot_and_probe():
    clock = FakeClock()
    a = make_stream_provider("a", chunks=tuple("abcdef"))
    router = make_router({"a": a}, clock=clock)
    gen = router.stream(request())
    assert await gen.__anext__() == "a"
    assert router.limiter.get_limiter("a").active_requests == 1
    await gen.aclose()  # client went away mid-stream
    assert router.limiter.get_limiter("a").active_requests == 0
    assert router.breakers["a"].state is CircuitState.CLOSED


@pytest.mark.asyncio
async def test_stream_disconnect_during_half_open_probe_frees_the_slot():
    clock = FakeClock()
    a = make_stream_provider("a", open_error=StatusError(500))
    router = make_router(
        {"a": a},
        clock=clock,
        circuit_failure_threshold=1,
        circuit_reset_seconds=30,
        max_retry_attempts=1,
    )
    with pytest.raises(StatusError):
        await collect(router.stream(request()))
    assert router.breakers["a"].state is CircuitState.OPEN
    clock.advance(31)

    a.stream = make_stream_provider("a", chunks=tuple("abc")).stream
    gen = router.stream(request())
    await gen.__anext__()  # this is the half-open probe
    await gen.aclose()
    assert router.breakers["a"].would_allow()


# --------------------------------------------------------------------------
# Configuration from the environment
# --------------------------------------------------------------------------


def test_routing_config_defaults_without_environment():
    from coreai.router import RoutingConfig

    cfg = RoutingConfig.from_env({})
    assert cfg.request_timeout_seconds == 60.0
    assert cfg.deadline_seconds == 120.0
    assert cfg.enable_failover is True
    assert cfg.provider_timeouts["deepseek"] == 120.0


def test_routing_config_reads_environment():
    from coreai.router import RoutingConfig

    cfg = RoutingConfig.from_env(
        {
            "REQUEST_TIMEOUT_SECONDS": "45",
            "PROVIDER_TIMEOUTS": "grok=90, deepseek=200",
            "REQUEST_DEADLINE_SECONDS": "30",
            "ENABLE_FAILOVER": "false",
            "MAX_ATTEMPTS_BEFORE_FAILOVER": "3",
            "CIRCUIT_FAILURE_THRESHOLD": "10",
            "CIRCUIT_RESET_SECONDS": "5.5",
            "CACHE_MAX_ENTRIES": "50",
        }
    )
    assert cfg.request_timeout_seconds == 45.0
    assert cfg.provider_timeouts == {"deepseek": 200.0, "grok": 90.0}
    assert cfg.deadline_seconds == 30.0
    assert cfg.enable_failover is False
    assert cfg.max_attempts_before_failover == 3
    assert cfg.circuit_failure_threshold == 10
    assert cfg.circuit_reset_seconds == 5.5
    assert cfg.cache_max_entries == 50


def test_deadline_can_be_disabled():
    from coreai.router import RoutingConfig

    assert (
        RoutingConfig.from_env({"REQUEST_DEADLINE_SECONDS": "none"}).deadline_seconds
        is None
    )


def test_overrides_beat_the_environment_and_blank_values_are_ignored():
    from coreai.router import RoutingConfig, RoutingStrategy

    cfg = RoutingConfig.from_env(
        {"REQUEST_TIMEOUT_SECONDS": "  "}, strategy=RoutingStrategy.CHEAPEST
    )
    assert cfg.strategy == RoutingStrategy.CHEAPEST
    assert cfg.request_timeout_seconds == 60.0


@pytest.mark.parametrize(
    "name,value",
    [
        ("REQUEST_TIMEOUT_SECONDS", "soon"),
        ("CIRCUIT_FAILURE_THRESHOLD", "3.5"),
        ("ENABLE_FAILOVER", "maybe"),
        ("PROVIDER_TIMEOUTS", "grok"),
    ],
)
def test_invalid_environment_values_fail_loudly_naming_the_variable(name, value):
    from coreai.router import RoutingConfig

    with pytest.raises(ValueError, match=name):
        RoutingConfig.from_env({name: value})


@pytest.mark.asyncio
async def test_status_distribution_is_recorded_per_provider_and_exported():
    from coreai.metrics import render_prometheus

    a = make_provider("a", behaviour=StatusError(529))
    b = make_provider("b")
    router = make_router(
        {"a": a, "b": b}, strategy=RoutingStrategy.FALLBACK, max_retry_attempts=1
    )
    await router.route(request())
    await router.route(request())
    stats = router.stats()
    assert stats["provider_stats"]["a"]["error_statuses"] == {"529": 2}
    assert stats["provider_stats"]["b"]["error_statuses"] == {}
    text = render_prometheus(stats)
    assert 'caips_provider_error_status_total{provider="a",status="529"} 2' in text


@pytest.mark.asyncio
async def test_retried_attempts_are_counted_by_status():
    a = make_provider("a", behaviour=[StatusError(529), StatusError(529), None])
    router = make_router({"a": a}, max_retry_attempts=3)
    await router.route(request())
    assert router.retry_manager.stats()["retries_by_status"] == {"529": 2}
