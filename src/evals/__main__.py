"""``python -m evals evals/ --base-url http://localhost:6969 --model fast [--baseline old.json]``

Runs the suite through a running gateway's OpenAI-compatible API, prints a summary,
writes JSON, and exits 1 on regressions against a baseline (for scheduled CI).
"""

import argparse
import asyncio
import json
import os
import sys
import time
from types import SimpleNamespace

import httpx

from providers.base import CompletionRequest, Message

from .runner import compare, load_tasks, regressions, run_suite


def http_route(client: httpx.AsyncClient):
    async def route(req):
        msgs = (
            [{"role": "system", "content": req.system_prompt}]
            if req.system_prompt
            else []
        ) + [{"role": m.role, "content": m.content} for m in req.messages]
        t = time.monotonic()
        r = await client.post(
            "/v1/chat/completions",
            json={
                "model": req.model,
                "messages": msgs,
                "max_tokens": req.max_tokens,
                "temperature": 0,
            },
        )
        r.raise_for_status()
        d = r.json()
        u = d.get("usage") or {}
        return SimpleNamespace(
            content=d["choices"][0]["message"]["content"] or "",
            cost_usd=float(r.headers.get("x-caips-cost-usd", 0) or 0),
            latency_ms=(time.monotonic() - t) * 1000,
            input_tokens=u.get("prompt_tokens", 0),
            output_tokens=u.get("completion_tokens", 0),
            provider=r.headers.get("x-caips-provider"),
            model=d.get("model"),
        )

    return route


def agent_route(client: httpx.AsyncClient, poll: float = 0.5, timeout: float = 300.0):
    """Run an agent through ``/v1/runs`` and wait for its answer."""

    async def route(task):
        t0 = time.monotonic()
        r = await client.post(
            "/v1/runs", json={"agent": task.agent, "task": task.prompt}
        )
        r.raise_for_status()
        rid = r.json()["id"]
        while True:
            d = (await client.get(f"/v1/runs/{rid}")).json()
            if d["status"] not in ("pending", "running"):
                break
            if time.monotonic() - t0 > timeout:
                raise TimeoutError(
                    f"run {rid} still {d['status']} after {timeout:.0f}s"
                )
            await asyncio.sleep(poll)
        if d["status"] != "completed":
            raise RuntimeError(f"run {d['status']}: {d.get('stop_reason')}")
        return SimpleNamespace(
            content=d.get("result") or "",
            cost_usd=float(d.get("cost_usd") or 0),
            latency_ms=(time.monotonic() - t0) * 1000,
            input_tokens=d.get("input_tokens", 0),
            output_tokens=d.get("output_tokens", 0),
            provider=None, model=None, run_id=rid, steps=d.get("step"),
        )  # fmt: skip

    return route


def model_grader(route, model):
    """A rubric grader: one completion on ``model`` through the gateway."""

    async def grade(prompt: str) -> str:
        req = CompletionRequest(
            messages=[Message(role="user", content=prompt)],
            model=model,
            max_tokens=20,
            temperature=0.0,
        )
        return (await route(req)).content

    return grade


def _with_model(req, model):
    req.model = model
    return req


def _model_runner(route, model):
    return lambda r: route(_with_model(r, model))


async def main(argv: list[str] | None = None, transport=None) -> int:
    ap = argparse.ArgumentParser(prog="evals")
    ap.add_argument("tasks")
    ap.add_argument("--base-url", default="http://localhost:6969")
    ap.add_argument("--model", default=None)
    ap.add_argument("--tag", action="append")
    ap.add_argument(
        "--compare",
        nargs="+",
        metavar="MODEL",
        help="run the same tasks on several models or aliases (e.g. cheap best local) and print a table",
    )
    ap.add_argument(
        "--grader-model",
        default=None,
        help="model that grades `rubric` tasks (through the same gateway)",
    )
    ap.add_argument("--out", default="eval-results.json")
    ap.add_argument("--baseline")
    ap.add_argument("--tolerance", type=float, default=0.05)
    a = ap.parse_args(argv)
    headers = (
        {"Authorization": f"Bearer {os.environ['CAIPS_API_KEY']}"}
        if os.getenv("CAIPS_API_KEY")
        else {}
    )
    async with httpx.AsyncClient(
        base_url=a.base_url, headers=headers, timeout=120, transport=transport
    ) as c:
        route = http_route(c)
        extra = {"agent_route": agent_route(c)}
        if a.grader_model:
            extra["grader"] = model_grader(route, a.grader_model)
        if a.compare:
            reps = {}
            for m in a.compare:
                reps[m] = (
                    await compare(
                        load_tasks(a.tasks),
                        {m: _model_runner(route, m)},
                        tags=a.tag,
                        **extra,
                    )
                )[m]
            json.dump(reps, open(a.out, "w"), indent=2)
            print(
                f"{'config':<16}{'quality':>8}{'errors':>7}{'cost $':>10}{'p50 ms':>8}{'p95 ms':>8}"
            )
            for m, r in reps.items():
                row = (
                    m,
                    r["quality"],
                    r["errors"],
                    r["cost_usd"],
                    r["p50_ms"],
                    r["p95_ms"],
                )
                print("%-16s%8.2f%7d%10.4f%8.0f%8.0f" % row)
            return 0
        rep = await run_suite(
            load_tasks(a.tasks), route, model=a.model, tags=a.tag, **extra
        )
    json.dump(rep, open(a.out, "w"), indent=2)
    print(f"{rep['n']} tasks  quality={rep['quality']:.2f}  errors={rep['errors']}  "
          f"cost=${rep['cost_usd']:.4f}  p50={rep['p50_ms']:.0f}ms  p95={rep['p95_ms']:.0f}ms")  # fmt: skip
    if a.baseline:
        bad = regressions(json.load(open(a.baseline)), rep, a.tolerance)
        if bad:
            print("REGRESSIONS:", ", ".join(bad))
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
