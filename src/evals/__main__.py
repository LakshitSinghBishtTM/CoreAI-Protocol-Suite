"""``python -m evals evals/ --base-url http://localhost:8000 --model fast [--baseline old.json]``

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

from .runner import load_tasks, regressions, run_suite


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


async def main(argv: list[str] | None = None, transport=None) -> int:
    ap = argparse.ArgumentParser(prog="evals")
    ap.add_argument("tasks")
    ap.add_argument("--base-url", default="http://localhost:8000")
    ap.add_argument("--model", default=None)
    ap.add_argument("--tag", action="append")
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
        rep = await run_suite(
            load_tasks(a.tasks), http_route(c), model=a.model, tags=a.tag
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
