#!/usr/bin/env python3
"""
CoreAI Protocol Suite - Benchmark Script
Runs a configurable load test against a live CoreAI server.
Reports latency percentiles, throughput, cost, and cache hit rate.

Usage:
    python scripts/benchmark.py --url http://localhost:6389 --requests 100
    python scripts/benchmark.py --provider deepseek --concurrency 5
"""

import argparse
import asyncio
import statistics
import time
from dataclasses import dataclass, field

import httpx


DEFAULT_URL = "http://localhost:6389"
DEFAULT_PROMPT = "Explain what an API is in one sentence."

PROMPTS = [
    "What is machine learning?",
    "Explain REST APIs in one sentence.",
    "What is a database index?",
    "Define latency in networking.",
    "What is a load balancer?",
    "Explain caching in one sentence.",
    "What is rate limiting?",
    "Define a microservice.",
    "What is a token in NLP?",
    "Explain exponential backoff.",
]


@dataclass
class BenchmarkResult:
    total_requests: int = 0
    successful: int = 0
    failed: int = 0
    cached: int = 0
    latencies: list[float] = field(default_factory=list)
    costs: list[float] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    start_time: float = field(default_factory=time.monotonic)
    end_time: float = 0.0

    def elapsed(self) -> float:
        return (self.end_time or time.monotonic()) - self.start_time

    def throughput(self) -> float:
        e = self.elapsed()
        return self.successful / e if e > 0 else 0

    def percentile(self, p: float) -> float:
        if not self.latencies:
            return 0.0
        sorted_l = sorted(self.latencies)
        idx = int(len(sorted_l) * p / 100)
        return sorted_l[min(idx, len(sorted_l) - 1)]

    def print_summary(self):
        print("\n" + "=" * 55)
        print("Benchmark Results")
        print("=" * 55)
        print(f"  Total requests  : {self.total_requests}")
        print(f"  Successful      : {self.successful}")
        print(f"  Failed          : {self.failed}")
        print(f"  Cached          : {self.cached}")
        print(f"  Elapsed         : {self.elapsed():.1f}s")
        print(f"  Throughput      : {self.throughput():.2f} req/s")
        if self.latencies:
            print(f"\n  Latency (ms)")
            print(f"    P50           : {self.percentile(50):.0f}ms")
            print(f"    P95           : {self.percentile(95):.0f}ms")
            print(f"    P99           : {self.percentile(99):.0f}ms")
            print(f"    Min           : {min(self.latencies):.0f}ms")
            print(f"    Max           : {max(self.latencies):.0f}ms")
        if self.costs:
            print(f"\n  Cost")
            print(f"    Total         : ${sum(self.costs):.6f}")
            print(f"    Avg/request   : ${statistics.mean(self.costs):.6f}")
        if self.errors:
            print(f"\n  Errors ({len(self.errors)} unique):")
            for e in self.errors[:5]:
                print(f"    - {e}")
        print("=" * 55)


async def single_request(
    client: httpx.AsyncClient,
    url: str,
    prompt: str,
    provider: str | None,
    result: BenchmarkResult,
    semaphore: asyncio.Semaphore,
):
    async with semaphore:
        payload = {
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 64,
            "temperature": 0.0,
        }
        if provider:
            payload["provider"] = provider

        try:
            resp = await client.post(f"{url}/v1/completions", json=payload, timeout=30)
            resp.raise_for_status()
            data = resp.json()

            result.successful += 1
            result.latencies.append(data.get("latency_ms", 0))
            result.costs.append(data.get("cost_usd", 0))
            if data.get("cached"):
                result.cached += 1

        except httpx.HTTPStatusError as e:
            result.failed += 1
            result.errors.append(f"HTTP {e.response.status_code}")
        except Exception as e:
            result.failed += 1
            result.errors.append(str(e)[:60])

        result.total_requests += 1


async def run_benchmark(
    url: str,
    n_requests: int,
    concurrency: int,
    provider: str | None,
):
    result = BenchmarkResult()
    semaphore = asyncio.Semaphore(concurrency)

    print(f"Benchmarking {url}")
    print(f"  Requests    : {n_requests}")
    print(f"  Concurrency : {concurrency}")
    print(f"  Provider    : {provider or 'auto'}")
    print()

    async with httpx.AsyncClient() as client:
        tasks = [
            single_request(
                client, url,
                PROMPTS[i % len(PROMPTS)],
                provider, result, semaphore,
            )
            for i in range(n_requests)
        ]
        await asyncio.gather(*tasks)

    result.end_time = time.monotonic()
    result.print_summary()


def main():
    parser = argparse.ArgumentParser(description="CoreAI benchmark tool")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--requests", type=int, default=50)
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--provider", default=None)
    args = parser.parse_args()

    asyncio.run(run_benchmark(args.url, args.requests, args.concurrency, args.provider))


if __name__ == "__main__":
    main()
