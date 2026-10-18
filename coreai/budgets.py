"""Per-subject spend and usage budgets.

Each API subject (the JWT ``sub``) gets limits on cost, requests and tokens
over a fixed window. A subject over its hard limit is refused with
``BudgetExceeded`` (HTTP 429 plus ``Retry-After`` at the API layer); crossing
the optional soft limit only sets a warning flag.

Notes on behaviour:
- Limits are checked before a request and usage is recorded after it, so one
  request that starts under the cap can finish over it. The next one is
  refused. Concurrent requests can overshoot by the cost of those in flight.
- A limit of 0 blocks everything; ``None`` (or omitted) means no limit.
- State is in memory and per process, and bounded to ``max_subjects`` (least
  recently used subjects are dropped). Restarting resets usage.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

WINDOWS = {"minute": 60, "hour": 3600, "day": 86400, "week": 604800}
_FIELDS = {"max_cost_usd", "warn_cost_usd", "max_requests", "max_tokens", "window"}


class BudgetConfigError(ValueError):
    pass


class BudgetExceeded(Exception):
    def __init__(self, limit: str, used: float, cap: float, retry_after: float):
        self.limit = limit
        self.used = used
        self.cap = cap
        self.retry_after = retry_after
        super().__init__(f"{limit} limit reached ({used:g} of {cap:g})")


@dataclass(frozen=True)
class BudgetLimits:
    max_cost_usd: float | None = None
    warn_cost_usd: float | None = None
    max_requests: int | None = None
    max_tokens: int | None = None
    window_seconds: int = 86400


@dataclass
class _Usage:
    window_start: float
    cost: float = 0.0
    requests: int = 0
    tokens: int = 0


class BudgetManager:
    def __init__(
        self,
        default: BudgetLimits | None = None,
        subjects: dict[str, BudgetLimits] | None = None,
        clock: Callable[[], float] = time.time,
        max_subjects: int = 10_000,
    ):
        self.default = default
        self.subjects = dict(subjects or {})
        self._clock = clock
        self._max_subjects = max_subjects
        self._usage: OrderedDict[str, _Usage] = OrderedDict()
        self.rejections = 0

    def limits_for(self, subject: str) -> BudgetLimits | None:
        return self.subjects.get(subject, self.default)

    def _current(self, subject: str, limits: BudgetLimits) -> _Usage:
        now = self._clock()
        start = now - (now % limits.window_seconds)
        usage = self._usage.get(subject)
        if usage is None or usage.window_start != start:
            usage = _Usage(window_start=start)
            self._usage[subject] = usage
        self._usage.move_to_end(subject)
        while len(self._usage) > self._max_subjects:
            self._usage.popitem(last=False)
        return usage

    def check(self, subject: str, estimated_cost: float = 0.0) -> None:
        """Raise BudgetExceeded if *subject* may not make another request."""
        limits = self.limits_for(subject)
        if limits is None:
            return
        u = self._current(subject, limits)
        retry = max(1.0, u.window_start + limits.window_seconds - self._clock())
        # (name, used, cap, amount this request would add)
        checks = (
            ("requests", u.requests, limits.max_requests, 1),
            ("tokens", u.tokens, limits.max_tokens, 0),
            ("cost_usd", u.cost, limits.max_cost_usd, estimated_cost),
        )
        for name, used, cap, extra in checks:
            if cap is None:
                continue
            # Tokens and cost are known only after a call, so an unused
            # budget of exactly 0 left (used >= cap) also blocks.
            if used + extra > cap or (extra == 0 and used >= cap):
                self.rejections += 1
                raise BudgetExceeded(name, used, cap, retry)

    def record(self, subject: str, cost_usd: float = 0.0, tokens: int = 0) -> None:
        limits = self.limits_for(subject)
        if limits is None:
            return
        u = self._current(subject, limits)
        u.requests += 1
        u.cost += max(0.0, cost_usd or 0.0)
        u.tokens += max(0, tokens or 0)

    def status(self, subject: str) -> dict[str, Any] | None:
        limits = self.limits_for(subject)
        if limits is None:
            return None
        u = self._current(subject, limits)
        warn = limits.warn_cost_usd
        return {
            "window_seconds": limits.window_seconds,
            "resets_in_seconds": round(
                u.window_start + limits.window_seconds - self._clock(), 1
            ),
            "used": {
                "cost_usd": round(u.cost, 6),
                "requests": u.requests,
                "tokens": u.tokens,
            },
            "limits": {
                "cost_usd": limits.max_cost_usd,
                "requests": limits.max_requests,
                "tokens": limits.max_tokens,
            },
            "warning": warn is not None and u.cost >= warn,
        }

    def stats(self) -> dict[str, Any]:
        return {"tracked_subjects": len(self._usage), "rejections": self.rejections}


def _parse(raw: Any, where: str, base: BudgetLimits, errors: list[str]) -> BudgetLimits:
    if not isinstance(raw, dict):
        errors.append(f"{where}: must be a mapping")
        return base
    for key in raw.keys() - _FIELDS:
        errors.append(f"{where}: unknown field '{key}'")
    out = base
    for key in ("max_cost_usd", "warn_cost_usd"):
        if key in raw:
            v = raw[key]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
                errors.append(f"{where}.{key}: must be a number >= 0")
            else:
                out = replace(out, **{key: float(v)})
    for key in ("max_requests", "max_tokens"):
        if key in raw:
            v = raw[key]
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                errors.append(f"{where}.{key}: must be an integer >= 0")
            else:
                out = replace(out, **{key: v})
    if "window" in raw:
        w = raw["window"]
        if isinstance(w, str) and w in WINDOWS:
            out = replace(out, window_seconds=WINDOWS[w])
        elif isinstance(w, int) and not isinstance(w, bool) and w > 0:
            out = replace(out, window_seconds=w)
        else:
            errors.append(
                f"{where}.window: use one of {sorted(WINDOWS)} or seconds > 0"
            )
    if (
        out.warn_cost_usd is not None
        and out.max_cost_usd is not None
        and out.warn_cost_usd > out.max_cost_usd
    ):
        errors.append(f"{where}: warn_cost_usd is above max_cost_usd")
    return out


def load_budgets(path: str, **kwargs: Any) -> BudgetManager:
    """Load limits from YAML. Reports every problem at once."""
    import yaml

    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise BudgetConfigError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise BudgetConfigError("top level must be a mapping")
    errors = [f"unknown top-level key '{k}'" for k in data.keys() - {"default", "subjects"}]
    default = None
    if "default" in data:
        default = _parse(data["default"], "default", BudgetLimits(), errors)
    subjects: dict[str, BudgetLimits] = {}
    raw_subjects = data.get("subjects") or {}
    if not isinstance(raw_subjects, dict):
        errors.append("subjects: must be a mapping")
        raw_subjects = {}
    for name, raw in raw_subjects.items():
        subjects[str(name)] = _parse(
            raw, f"subjects.{name}", default or BudgetLimits(), errors
        )
    if errors:
        raise BudgetConfigError("; ".join(errors))
    return BudgetManager(default, subjects, **kwargs)
