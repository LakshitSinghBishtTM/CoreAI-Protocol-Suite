#!/usr/bin/env python3
"""
CoreAI Protocol Suite - Database Seeder
Populates the database with realistic sample data for development and testing.
Inserts request logs, usage stats, agents, tasks, and API keys.

Usage:
    python scripts/seed_db.py
    python scripts/seed_db.py --reset    # drop and recreate tables first
"""

import argparse
import asyncio
import hashlib
import os
import random
import sys
import uuid
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from database.db import get_session, init_db
from database.migrations import create_all_tables
from database.models import Agent, APIKey, Base, RequestLog, Task, UsageDaily

PROVIDERS = ["openai", "anthropic", "gemini", "deepseek", "grok"]
MODELS = {
    "openai": ["gpt-4o-mini", "gpt-4o"],
    "anthropic": ["claude-haiku-4-5", "claude-sonnet-4-5"],
    "gemini": ["gemini-2.0-flash", "gemini-1.5-pro"],
    "deepseek": ["deepseek-chat"],
    "grok": ["grok-2-mini"],
}
STRATEGIES = ["balanced", "cheapest", "fastest"]
AGENT_IDS = [
    "research-bot-1",
    "summarizer-agent",
    "code-reviewer-1",
    "data-analyst-bot",
]


def random_cost(provider: str) -> tuple[float, int, int]:
    base = {
        "openai": 0.00015,
        "anthropic": 0.00012,
        "gemini": 0.000008,
        "deepseek": 0.0000009,
        "grok": 0.00009,
    }
    cost = base.get(provider, 0.0001) * random.uniform(0.5, 3.0)
    input_t = random.randint(40, 800)
    output_t = random.randint(20, 300)
    return round(cost, 8), input_t, output_t


def random_date(days_back: int = 30) -> datetime:
    return datetime.utcnow() - timedelta(
        days=random.randint(0, days_back),
        hours=random.randint(0, 23),
        minutes=random.randint(0, 59),
    )


async def seed_request_logs(session, n: int = 500):
    print(f"  Seeding {n} request logs...")
    logs = []
    for _ in range(n):
        provider = random.choice(PROVIDERS)
        model = random.choice(MODELS[provider])
        cost, input_t, output_t = random_cost(provider)
        logs.append(
            RequestLog(
                id=str(uuid.uuid4()),
                provider=provider,
                model=model,
                strategy=random.choice(STRATEGIES),
                input_tokens=input_t,
                output_tokens=output_t,
                cost_usd=cost,
                latency_ms=round(random.uniform(180, 3200), 1),
                cached=random.random() < 0.15,
                success=random.random() > 0.02,
                agent_id=random.choice(AGENT_IDS + [None, None, None]),
                created_at=random_date(30),
            )
        )
    session.add_all(logs)
    print(f"  Done.")


async def seed_usage_daily(session, days: int = 30):
    print(f"  Seeding {days} days of usage stats...")
    for day_offset in range(days):
        day = (datetime.utcnow() - timedelta(days=day_offset)).date()
        for provider in PROVIDERS:
            reqs = random.randint(10, 400)
            failed = random.randint(0, max(1, reqs // 50))
            cached = random.randint(0, reqs // 8)
            _, input_t, output_t = random_cost(provider)
            session.add(
                UsageDaily(
                    date=day.isoformat(),
                    provider=provider,
                    total_requests=reqs,
                    successful_requests=reqs - failed,
                    failed_requests=failed,
                    cached_requests=cached,
                    total_input_tokens=input_t * reqs,
                    total_output_tokens=output_t * reqs,
                    total_cost_usd=round(random_cost(provider)[0] * reqs, 6),
                    avg_latency_ms=round(random.uniform(200, 1800), 1),
                    p95_latency_ms=round(random.uniform(1200, 4500), 1),
                )
            )
    print(f"  Done.")


async def seed_agents_and_tasks(session):
    print(f"  Seeding {len(AGENT_IDS)} agents and tasks...")
    statuses = ["completed", "completed", "completed", "failed", "pending"]
    for agent_id in AGENT_IDS:
        session.add(
            Agent(
                id=agent_id,
                display_name=agent_id.replace("-", " ").title(),
                config={"max_iterations": 20, "strategy": "balanced"},
                total_tasks=random.randint(5, 40),
                completed_tasks=random.randint(3, 35),
                failed_tasks=random.randint(0, 3),
                created_at=random_date(60),
                last_seen_at=random_date(2),
            )
        )
        for _ in range(random.randint(2, 6)):
            status = random.choice(statuses)
            started = random_date(14)
            session.add(
                Task(
                    id=str(uuid.uuid4()),
                    agent_id=agent_id,
                    objective=random.choice(
                        [
                            "Analyze Q3 market trends for enterprise AI sector",
                            "Summarize latest research papers on transformer architectures",
                            "Review codebase for potential security issues",
                            "Generate weekly cost report across all providers",
                            "Benchmark response quality across providers",
                        ]
                    ),
                    status=status,
                    result=(
                        "Task completed successfully."
                        if status == "completed"
                        else None
                    ),
                    error=(
                        "Provider rate limit exceeded." if status == "failed" else None
                    ),
                    iterations=random.randint(1, 18),
                    total_cost_usd=round(random.uniform(0.001, 0.08), 6),
                    created_at=started,
                    started_at=started + timedelta(seconds=random.randint(1, 10)),
                    completed_at=(
                        started + timedelta(minutes=random.randint(1, 30))
                        if status in ("completed", "failed")
                        else None
                    ),
                )
            )
    print(f"  Done.")


async def seed_api_keys(session):
    print(f"  Seeding API keys...")
    keys = [
        ("dev-local", "lakshit", "coreai-dev-Xm7kQ2nP4vL9wR5j"),
        ("prod-svc", "lakshit", "coreai-prod-Tz1Yw9Lc3Fh6Jd5B"),
        ("test-ci", "ci-runner", "coreai-test-Nq8Rv4Kp2Xm7Tz1Y"),
    ]
    for label, owner, raw_key in keys:
        session.add(
            APIKey(
                id=str(uuid.uuid4()),
                key_hash=hashlib.sha256(raw_key.encode()).hexdigest(),
                label=label,
                owner=owner,
                active=True,
                requests_made=random.randint(0, 2000),
                created_at=random_date(90),
            )
        )
    print(f"  Done.")


async def main(reset: bool = False):
    init_db(echo=False)

    if reset:
        print("Resetting tables...")
        await create_all_tables()

    print("Seeding database...")
    async with get_session() as session:
        await seed_request_logs(session)
        await seed_usage_daily(session)
        await seed_agents_and_tasks(session)
        await seed_api_keys(session)

    print("\nSeed complete.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--reset", action="store_true", help="Drop and recreate tables first"
    )
    args = parser.parse_args()
    asyncio.run(main(reset=args.reset))
