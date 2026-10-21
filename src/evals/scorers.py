"""Scorers return 0.0..1.0. ``rubric`` needs a grader (a model call) and is async."""

import json
import re
from typing import Any, Awaitable, Callable

Grader = Callable[[str], Awaitable[str]]  # prompt -> grader's text


def _norm(s: str) -> str:
    return " ".join(s.lower().split())


async def score(
    spec: dict[str, Any], output: str, grader: Grader | None = None
) -> float:
    kind = spec.get("type", "exact")
    if kind == "exact":
        return float(_norm(output) == _norm(str(spec["expected"])))
    if kind == "contains":
        want = spec["expected"]
        want = [want] if isinstance(want, str) else want
        return sum(_norm(w) in _norm(output) for w in want) / len(want)
    if kind == "regex":
        return float(re.search(spec["pattern"], output, re.S) is not None)
    if kind == "json":
        try:
            data = json.loads(output)
        except ValueError:
            return 0.0
        want = spec.get("expected")
        if want is None:
            return 1.0
        return float(all(data.get(k) == v for k, v in want.items()))
    if kind == "rubric":
        if grader is None:
            raise ValueError("rubric scoring needs a grader")
        prompt = (
            "Grade the ANSWER against the RUBRIC. Reply with only a number from 0 to 10.\n"
            f"RUBRIC: {spec['rubric']}\nANSWER: {output}"
        )
        m = re.search(r"\d+(\.\d+)?", await grader(prompt))
        return min(float(m.group()), 10.0) / 10.0 if m else 0.0
    raise ValueError(f"unknown scorer '{kind}'")
