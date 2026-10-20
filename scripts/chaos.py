#!/usr/bin/env python
"""Chaos check for the gateway's failover behaviour. No network, no API keys.

For every provider in turn, and for every fault type, this runs the real
Router against fake providers in three phases:

  1. baseline  - healthy traffic, everything must succeed
  2. fault     - that one provider is killed / slowed / errors; the gateway
                 must keep serving from the others
  3. recovery  - the fault is removed; after the circuit-breaker reset time
                 the provider must receive traffic again

A final scenario takes every provider down at once and checks that requests
fail fast with a clear error, then recover.

Usage:
    python scripts/chaos.py                 # table, exit 1 on any failure
    python scripts/chaos.py --json          # machine-readable
    python scripts/chaos.py --requests 300 --min-success 0.995

What is checked, and the limits, are documented in docs/operations.md.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from coreai.errors import AllProvidersUnavailable
from coreai.limiter import RateLimitConfig
from coreai.router import Router, RoutingConfig, RoutingStrategy
from providers.base import (
    CompletionRequest,
    CompletionResponse,
    Message,
)

PROVIDERS = ["alpha", "bravo", "charlie"]
PROVIDER_TIMEOUT_S = 0.25
CIRCUIT_RESET_S = 0.4
SLOW_DELAY_S = 5.0  # far beyond the timeout, so a slow call is always cut off


class HTTPFault(Exception):
    """Looks like an SDK status error (status_code + response.headers)."""

    def __init__(self, status: int, retry_after: float | None = None):
        super().__init__(f"HTTP {status}")
        self.status_code = status
        headers = {"retry-after": str(retry_after)} if retry_after else {}
        self.response = SimpleNamespace(status_code=status, headers=headers)


FAULTS = {
    "down": lambda: ConnectionError("connection refused"),
    "http_500": lambda: HTTPFault(500),
    "overloaded_529": lambda: HTTPFault(529, retry_after=0.05),
    "rate_limited_429": lambda: HTTPFault(429, retry_after=0.05),
    "flaky_50pct": None,  # handled separately
    "slow": None,  # handled separately
}


class FakeProvider:
    def __init__(self, name: str, rng: random.Random):
        self.name = name
        self.default_model = f"{name}-model"
        self.fault: str | None = None
        self.calls = 0
        self.served = 0
        self._rng = rng

    def estimate_cost(self, input_tokens, output_tokens, model) -> float:
        return 0.001

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.calls += 1
        fault = self.fault
        if fault == "slow":
            await asyncio.sleep(SLOW_DELAY_S)
        elif fault == "flaky_50pct":
            await asyncio.sleep(0.002)
            if self._rng.random() < 0.5:
                raise HTTPFault(500)
        elif fault is not None:
            await asyncio.sleep(0.001)
            raise FAULTS[fault]()
        else:
            await asyncio.sleep(0.002)
        self.served += 1
        return CompletionResponse(
            content="ok",
            model=self.default_model,
            provider=self.name,
            input_tokens=10,
            output_tokens=5,
            cost_usd=0.001,
            latency_ms=2.0,
        )


@dataclass
class PhaseResult:
    sent: int = 0
    ok: int = 0
    errors: dict[str, int] = field(default_factory=dict)
    p95_ms: float = 0.0
    served_by: dict[str, int] = field(default_factory=dict)

    @property
    def success_rate(self) -> float:
        return self.ok / self.sent if self.sent else 1.0


@dataclass
class ScenarioResult:
    provider: str
    fault: str
    baseline: PhaseResult
    during: PhaseResult
    recovery: PhaseResult
    failures: list[str]

    @property
    def passed(self) -> bool:
        return not self.failures


def build(order: list[str], seed: int = 1) -> tuple[Router, dict[str, FakeProvider]]:
    rng = random.Random(seed)
    fakes = {n: FakeProvider(n, rng) for n in order}
    config = RoutingConfig(
        strategy=RoutingStrategy.FALLBACK,  # first in `order` is the primary
        enable_cache=False,
        max_retry_attempts=2,
        max_attempts_before_failover=1,
        retry_initial_wait_seconds=0.005,
        retry_max_wait_seconds=0.05,
        provider_timeouts={n: PROVIDER_TIMEOUT_S for n in order},
        request_timeout_seconds=PROVIDER_TIMEOUT_S,
        deadline_seconds=3.0,
        circuit_failure_threshold=3,
        circuit_reset_seconds=CIRCUIT_RESET_S,
        health_half_life_seconds=0.3,
        # Load tests must not be throttled by the local limiter.
        rate_limits={
            n: RateLimitConfig(
                requests_per_minute=10**6,
                tokens_per_minute=10**9,
                concurrent_requests=1000,
            )
            for n in order
        },
    )
    return Router(fakes, config), fakes


async def run_load(
    router: Router, fakes: dict[str, FakeProvider], n: int, concurrency: int
) -> PhaseResult:
    before = {name: p.served for name, p in fakes.items()}
    res = PhaseResult()
    latencies: list[float] = []
    sem = asyncio.Semaphore(concurrency)
    request = CompletionRequest(messages=[Message(role="user", content="hi")])

    async def one():
        async with sem:
            start = time.perf_counter()
            try:
                await router.route(request)
                res.ok += 1
            except AllProvidersUnavailable:
                res.errors["all_providers_unavailable"] = (
                    res.errors.get("all_providers_unavailable", 0) + 1
                )
            except Exception as exc:  # noqa: BLE001 - report every kind
                key = type(exc).__name__
                res.errors[key] = res.errors.get(key, 0) + 1
            finally:
                latencies.append((time.perf_counter() - start) * 1000)
            res.sent += 1

    await asyncio.gather(*(one() for _ in range(n)))
    latencies.sort()
    res.p95_ms = latencies[int(0.95 * (len(latencies) - 1))] if latencies else 0.0
    res.served_by = {
        name: p.served - before[name]
        for name, p in fakes.items()
        if p.served - before[name]
    }
    return res


async def wait_until_serving(router, fakes, timeout: float = 3.0) -> float:
    """Send single requests until one succeeds; returns seconds taken.

    After an outage every breaker is half-open and admits one probe at a time,
    so a burst arriving in the same millisecond mostly gets fast 503s by
    design. Real clients retry; this does the same before measuring."""
    start = time.perf_counter()
    while time.perf_counter() - start < timeout:
        if (await run_load(router, fakes, 1, 1)).ok:
            break
        await asyncio.sleep(0.05)
    return time.perf_counter() - start


async def run_scenario(
    target: str,
    fault: str,
    requests: int,
    concurrency: int,
    min_success: float,
    max_p95_ms: float,
) -> ScenarioResult:
    order = [target] + [p for p in PROVIDERS if p != target]
    router, fakes = build(order)
    failures: list[str] = []

    baseline = await run_load(router, fakes, requests // 3, concurrency)
    if baseline.success_rate < 1.0:
        failures.append(f"baseline not clean: {baseline.errors}")

    fakes[target].fault = fault
    during = await run_load(router, fakes, requests, concurrency)
    if during.success_rate < min_success:
        failures.append(
            f"success {during.success_rate:.1%} < {min_success:.1%} "
            f"while {target} is {fault} ({during.errors})"
        )
    if during.p95_ms > max_p95_ms:
        failures.append(f"p95 {during.p95_ms:.0f}ms > {max_p95_ms:.0f}ms during fault")

    fakes[target].fault = None
    await asyncio.sleep(CIRCUIT_RESET_S + 0.2)  # breaker reset
    await wait_until_serving(router, fakes)
    recovery = await run_load(router, fakes, requests // 3, concurrency)
    if recovery.success_rate < 1.0:
        failures.append(f"recovery not clean: {recovery.errors}")
    if recovery.served_by.get(target, 0) == 0:
        failures.append(f"{target} never got traffic back after the fault ended")

    return ScenarioResult(target, fault, baseline, during, recovery, failures)


async def run_total_outage(requests: int, concurrency: int) -> ScenarioResult:
    router, fakes = build(list(PROVIDERS))
    failures: list[str] = []
    baseline = await run_load(router, fakes, requests // 3, concurrency)
    for p in fakes.values():
        p.fault = "down"
    during = await run_load(router, fakes, requests, concurrency)
    if during.ok:
        failures.append("requests succeeded with every provider down")
    if during.p95_ms > 1500:
        failures.append(f"outage errors are slow (p95 {during.p95_ms:.0f}ms)")
    unexpected = set(during.errors) - {
        "all_providers_unavailable",
        "ConnectionError",
    }
    if unexpected:
        failures.append(f"unexpected error types: {sorted(unexpected)}")
    for p in fakes.values():
        p.fault = None
    await asyncio.sleep(CIRCUIT_RESET_S + 0.2)
    took = await wait_until_serving(router, fakes)
    if took > 2.0:
        failures.append(f"took {took:.1f}s to serve again after the outage")
    recovery = await run_load(router, fakes, requests // 3, concurrency)
    if recovery.success_rate < 1.0:
        failures.append(f"did not recover after outage: {recovery.errors}")
    return ScenarioResult("*all*", "down", baseline, during, recovery, failures)


async def run_all(args) -> list[ScenarioResult]:
    results = []
    for target in PROVIDERS:
        for fault in FAULTS:
            results.append(
                await run_scenario(
                    target,
                    fault,
                    args.requests,
                    args.concurrency,
                    args.min_success,
                    args.max_p95_ms,
                )
            )
    results.append(await run_total_outage(args.requests, args.concurrency))
    return results


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--requests", type=int, default=150, help="requests per fault phase"
    )
    ap.add_argument("--concurrency", type=int, default=10)
    ap.add_argument("--min-success", type=float, default=0.99)
    ap.add_argument("--max-p95-ms", type=float, default=2000.0)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    results = asyncio.run(run_all(args))
    if args.json:
        print(json.dumps([asdict(r) | {"passed": r.passed} for r in results], indent=2))
    else:
        print(f"{'provider':9} {'fault':17} {'success':>8} {'p95 ms':>8}  result")
        for r in results:
            status = "ok" if r.passed else "FAIL: " + "; ".join(r.failures)
            print(
                f"{r.provider:9} {r.fault:17} {r.during.success_rate:8.1%} "
                f"{r.during.p95_ms:8.0f}  {status}"
            )
        p95s = [r.during.p95_ms for r in results]
        print(
            f"\n{sum(r.passed for r in results)}/{len(results)} scenarios passed "
            f"(median fault p95 {statistics.median(p95s):.0f}ms)"
        )
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
