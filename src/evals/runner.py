"""Run a task suite against one or more configs and compare them."""

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean
from typing import Any, Awaitable, Callable

import yaml

from providers.base import CompletionRequest, Message

from .scorers import score

Route = Callable[[CompletionRequest], Awaitable[Any]]


@dataclass
class Task:
    id: str
    prompt: str
    scorer: dict
    system: str | None = None
    tags: list[str] = field(default_factory=list)
    max_tokens: int = 512
    agent: str | None = (
        None  # run this agent (via the run API) instead of one completion
    )


def load_tasks(path: str | Path) -> list[Task]:
    """YAML: ``tasks: [{id, prompt, scorer: {...}, system?, tags?}]`` (a dir loads all *.yml)."""
    p = Path(path)
    files = sorted(p.glob("*.yml")) if p.is_dir() else [p]
    tasks, seen = [], set()
    for f in files:
        for t in (yaml.safe_load(f.read_text()) or {}).get("tasks", []):
            if t["id"] in seen:
                raise ValueError(f"duplicate task id '{t['id']}'")
            seen.add(t["id"])
            tasks.append(Task(**t))
    return tasks


async def run_suite(
    tasks: list[Task],
    route: Route,
    *,
    model: str | None = None,
    metadata: dict | None = None,
    grader: Any = None,
    agent_route: Callable[[Task], Awaitable[Any]] | None = None,
    concurrency: int = 4,
    tags: list[str] | None = None,
) -> dict:
    """Run every task once. Errors count as score 0 and are recorded, never raised."""
    sem = asyncio.Semaphore(concurrency)
    chosen = [t for t in tasks if not tags or set(tags) & set(t.tags)]

    async def one(t: Task) -> dict:
        if t.agent:
            return await one_agent(t)
        req = CompletionRequest(
            messages=[Message(role="user", content=t.prompt)],
            model=model,
            system_prompt=t.system,
            max_tokens=t.max_tokens,
            temperature=0.0,
            metadata=dict(metadata or {}),
        )
        async with sem:
            start = time.monotonic()
            try:
                r = await route(req)
                sc = await score(t.scorer, r.content, grader)
                return {
                    "id": t.id, "score": sc, "cost_usd": r.cost_usd,
                    "latency_ms": r.latency_ms or (time.monotonic() - start) * 1000,
                    "tokens": (r.input_tokens or 0) + (r.output_tokens or 0),
                    "provider": r.provider, "model": r.model, "error": None,
                }  # fmt: skip
            except Exception as e:
                return {
                    "id": t.id, "score": 0.0, "cost_usd": 0.0, "latency_ms": 0.0,
                    "tokens": 0, "provider": None, "model": None, "error": str(e),
                }  # fmt: skip

    async def one_agent(t: Task) -> dict:
        async with sem:
            start = time.monotonic()
            try:
                if agent_route is None:
                    raise ValueError("agent tasks need a gateway with the run API")
                r = await agent_route(t)
                sc = await score(t.scorer, r.content, grader)
                return {
                    "id": t.id, "score": sc, "cost_usd": r.cost_usd,
                    "latency_ms": r.latency_ms or (time.monotonic() - start) * 1000,
                    "tokens": (r.input_tokens or 0) + (r.output_tokens or 0),
                    "provider": r.provider, "model": r.model, "error": None,
                    "agent": t.agent, "run_id": getattr(r, "run_id", None),
                    "steps": getattr(r, "steps", None),
                }  # fmt: skip
            except Exception as e:
                return {
                    "id": t.id, "score": 0.0, "cost_usd": 0.0, "latency_ms": 0.0,
                    "tokens": 0, "provider": None, "model": None, "error": str(e),
                    "agent": t.agent, "run_id": None, "steps": None,
                }  # fmt: skip

    results = await asyncio.gather(*(one(t) for t in chosen))
    lat = sorted(r["latency_ms"] for r in results if not r["error"])
    return {
        "n": len(results),
        "quality": mean(r["score"] for r in results) if results else 0.0,
        "errors": sum(1 for r in results if r["error"]),
        "cost_usd": sum(r["cost_usd"] for r in results),
        "p50_ms": lat[len(lat) // 2] if lat else 0.0,
        "p95_ms": lat[min(len(lat) - 1, int(len(lat) * 0.95))] if lat else 0.0,
        "results": results,
    }


async def compare(tasks: list[Task], configs: dict[str, Route], **kw) -> dict:
    """Same tasks, several configs (models, routing policies, local vs cloud)."""
    out = {}
    for name, route in configs.items():
        out[name] = await run_suite(tasks, route, **kw)
    return out


def regressions(base: dict, new: dict, tol: float = 0.05) -> list[str]:
    """Task ids whose score dropped by more than ``tol`` versus a baseline run."""
    old = {r["id"]: r["score"] for r in base["results"]}
    return [
        r["id"]
        for r in new["results"]
        if r["id"] in old and old[r["id"]] - r["score"] > tol
    ]
