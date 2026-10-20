"""tools/replay.py

Tool test harness: record real calls to a JSONL file, then replay them
against the registry to catch regressions. Recording stores argument values
and output, so it is opt-in and meant for development, not production.
"""

import json
from pathlib import Path
from typing import Optional

from .registry import ToolError, ToolRegistry, ToolResult


class Recorder:
    """Use as ``registry.on_call = Recorder(path)``."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def __call__(self, caller, tool, arguments, outcome, duration_ms) -> None:
        ok = isinstance(outcome, ToolResult)
        entry = {
            "tool": tool, "arguments": arguments, "ok": ok,
            "output": outcome.content if ok else None,
            "error": None if ok else getattr(outcome, "code", "error"),
        }  # fmt: skip
        with self.path.open("a") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")


async def replay(registry: ToolRegistry, path: str | Path, caller: str = "replay") -> list[dict]:
    """Re-run recorded calls; return the mismatches (empty list = no regressions)."""
    problems = []
    for n, line in enumerate(Path(path).read_text().splitlines(), 1):
        if not line.strip():
            continue
        rec = json.loads(line)
        try:
            res = await registry.call(
                rec["tool"], rec["arguments"], allowed=["*"], caller=caller, approved=True
            )
            got = {"ok": True, "output": res.content, "error": None}
        except ToolError as exc:
            got = {"ok": False, "output": None, "error": exc.code}
        want = {"ok": rec["ok"], "output": rec["output"], "error": rec["error"]}
        if got != want:
            problems.append({"line": n, "tool": rec["tool"], "expected": want, "got": got})
    return problems
