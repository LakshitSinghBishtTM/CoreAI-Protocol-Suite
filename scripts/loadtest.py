#!/usr/bin/env python
"""Load test: sustained traffic with faults, checking memory stays bounded.

Runs the real Router against fake providers (no network) with the response
cache on, unique prompts (so the cache keeps filling), and a mix of healthy,
slow and failing providers. tracemalloc compares memory after warm-up with
memory at the end. Steady growth after warm-up means something is unbounded;
the top growing source lines are printed.

Usage:
    python scripts/loadtest.py                    # 20,000 requests
    python scripts/loadtest.py --requests 100000 --max-growth-mb 5
    python scripts/loadtest.py --json

Exit code 1 if growth after warm-up exceeds --max-growth-mb, a bounded
structure exceeds its limit, or the error rate is above --max-error-rate.
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import json
import sys
import time
import tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import chaos

from coreai.router import RoutingConfig, RoutingStrategy
from providers.base import CompletionRequest, Message


def build_router(cache_entries: int):
    router, fakes = chaos.build(list(chaos.PROVIDERS))
    cfg: RoutingConfig = router.config
    # Rebuild with the cache on and a size limit, same other settings.
    from coreai.router import Router

    cfg.enable_cache = True
    cfg.cache_max_entries = cache_entries
    cfg.strategy = RoutingStrategy.BALANCED
    # The chaos script uses 250ms timeouts; here the event loop itself is slowed
    # by memory tracing, so use realistic ones or healthy providers time out.
    cfg.provider_timeouts = {n: 5.0 for n in chaos.PROVIDERS}
    cfg.request_timeout_seconds = 5.0
    cfg.deadline_seconds = 15.0
    router = Router(fakes, cfg)
    # A permanently flaky provider keeps errors, breakers and failover busy.
    fakes["charlie"].fault = "flaky_50pct"
    fakes["alpha"].estimate_cost = lambda *a: 0.003  # spread traffic over all three
    return router, fakes


async def drive(router, start: int, count: int, concurrency: int) -> tuple[int, dict]:
    """Send *count* unique requests through a fixed pool of workers (so the
    harness itself holds O(concurrency) tasks, not O(count))."""
    ok = 0
    errors: dict[str, int] = {}
    counter = iter(range(start, start + count))

    async def worker():
        nonlocal ok
        for i in counter:  # shared iterator: each number is taken once
            req = CompletionRequest(
                messages=[Message(role="user", content=f"prompt number {i}")]
            )
            try:
                await router.route(req)
                ok += 1
            except Exception as exc:  # noqa: BLE001
                key = type(exc).__name__
                errors[key] = errors.get(key, 0) + 1

    await asyncio.gather(*(worker() for _ in range(concurrency)))
    return ok, errors


def bounded_structures(router, cache_entries: int) -> dict[str, tuple[int, int]]:
    """name -> (current size, limit)"""
    out = {}
    if router.cache is not None:
        backend = getattr(router.cache, "backend", None) or router.cache
        store = getattr(backend, "store", None)
        if store is not None:
            out["cache entries"] = (len(store), cache_entries)
    for name, st in router.provider_stats.items():
        out[f"{name} latency history"] = (len(st["latency_ms_history"]), 1000)
    for name, h in router.health.items():
        out[f"{name} health events"] = (h.sample_count(), 200)
    return out


async def run(args) -> dict:
    from loguru import logger

    logger.remove()  # per-request log lines would dominate the run
    router, fakes = build_router(args.cache_entries)
    warm = max(1, args.requests // 4)

    tracemalloc.start(5)
    await drive(router, 0, warm, args.concurrency)
    gc.collect()
    baseline = tracemalloc.take_snapshot()
    base_mem = tracemalloc.get_traced_memory()[0]

    t0 = time.perf_counter()
    ok, error_kinds = await drive(router, warm, args.requests - warm, args.concurrency)
    errors = sum(error_kinds.values())
    elapsed = time.perf_counter() - t0
    gc.collect()
    end_mem = tracemalloc.get_traced_memory()[0]
    final = tracemalloc.take_snapshot()
    tracemalloc.stop()

    growth_mb = (end_mem - base_mem) / 1e6
    top = [
        {"where": str(s.traceback[0]), "growth_kb": round(s.size_diff / 1024, 1)}
        for s in final.compare_to(baseline, "lineno")[:5]
        if s.size_diff > 0
    ]
    sizes = bounded_structures(router, args.cache_entries)
    over = {k: v for k, v in sizes.items() if v[0] > v[1]}
    total = ok + errors
    result = {
        "requests": args.requests,
        "measured_requests": total,
        "requests_per_second": round(total / elapsed) if elapsed else None,
        "error_rate": round(errors / total, 4) if total else 0.0,
        "error_kinds": error_kinds,
        "memory_growth_mb": round(growth_mb, 2),
        "top_growth": top,
        "bounded_structures": {
            k: {"size": a, "limit": b} for k, (a, b) in sizes.items()
        },
        "failures": [],
    }
    if growth_mb > args.max_growth_mb:
        result["failures"].append(
            f"memory grew {growth_mb:.1f}MB after warm-up (limit {args.max_growth_mb}MB)"
        )
    for name, (size, limit) in over.items():
        result["failures"].append(f"{name} is {size}, over its limit of {limit}")
    if result["error_rate"] > args.max_error_rate:
        result["failures"].append(
            f"error rate {result['error_rate']:.2%} above {args.max_error_rate:.2%}"
        )
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--requests", type=int, default=20_000)
    ap.add_argument("--concurrency", type=int, default=50)
    ap.add_argument("--cache-entries", type=int, default=500)
    ap.add_argument("--max-growth-mb", type=float, default=5.0)
    ap.add_argument("--max-error-rate", type=float, default=0.01)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    result = asyncio.run(run(args))
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(
            f"{result['measured_requests']} requests after warm-up, "
            f"{result['requests_per_second']} req/s, "
            f"error rate {result['error_rate']:.2%} {result['error_kinds']}, "
            f"memory growth {result['memory_growth_mb']}MB"
        )
        for name, v in result["bounded_structures"].items():
            if name.startswith("cache") or name.startswith("alpha"):
                print(f"  {name}: {v['size']} / {v['limit']}")
        for t in result["top_growth"]:
            print(f"  grew {t['growth_kb']}KB at {t['where']}")
        for f in result["failures"]:
            print(f"FAIL: {f}")
        if not result["failures"]:
            print("ok")
    return 1 if result["failures"] else 0


if __name__ == "__main__":
    sys.exit(main())
