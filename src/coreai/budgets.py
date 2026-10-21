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

import math
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field, replace
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
    alerted: set = field(default_factory=set)  # alert kinds sent this window


class BudgetManager:
    def __init__(
        self,
        default: BudgetLimits | None = None,
        subjects: dict[str, BudgetLimits] | None = None,
        models: dict[str, BudgetLimits] | None = None,
        clock: Callable[[], float] = time.time,
        max_subjects: int = 10_000,
    ):
        self.default = default
        self.subjects = dict(subjects or {})
        # Shared caps per model, across all subjects (see check_model).
        self.models = dict(models or {})
        self._clock = clock
        self._max_subjects = max_subjects
        self._usage: OrderedDict[str, _Usage] = OrderedDict()
        self.rejections = 0
        # Called with an event dict on the first warning / exceeded per window.
        self.on_alert: Callable[[dict], None] | None = None

    def _alert(self, key: str, kind: str, u: _Usage, **extra: Any) -> None:
        if self.on_alert is None or kind in u.alerted:
            return
        u.alerted.add(kind)
        is_model = key.startswith("\x00model:")
        event = {
            "event": f"budget.{kind}",
            ("model" if is_model else "subject"): (
                key.split(":", 1)[1] if is_model else key
            ),
            "used": {
                "cost_usd": round(u.cost, 6),
                "requests": u.requests,
                "tokens": u.tokens,
            },
            "window_start": u.window_start,
            **extra,
        }
        try:
            self.on_alert(event)
        except Exception:  # noqa: BLE001 - alerting must never break accounting
            pass

    def _warn_check(self, key: str, limits: BudgetLimits, u: _Usage) -> None:
        if limits.warn_cost_usd is not None and u.cost >= limits.warn_cost_usd:
            self._alert(key, "warning", u, warn_cost_usd=limits.warn_cost_usd)

    def limits_for(self, subject: str) -> BudgetLimits | None:
        return self.subjects.get(subject, self.default)

    @staticmethod
    def _model_key(model: str) -> str:
        return "\x00model:" + model  # cannot collide with a JWT subject

    def check_model(self, model: str | None) -> None:
        """Raise BudgetExceeded if *model* has used up its shared cap.
        Unknown or unset models are never blocked here."""
        limits = self.models.get(model or "")
        if limits is not None:
            self._check(self._model_key(model), limits, 0.0)

    def record_model(self, model: str | None, cost_usd=0.0, tokens=0, n=1) -> None:
        limits = self.models.get(model or "")
        if limits is None:
            return
        u = self._current(self._model_key(model), limits)
        u.requests += n
        u.cost += max(0.0, cost_usd or 0.0)
        u.tokens += max(0, tokens or 0)
        self._warn_check(self._model_key(model), limits, u)

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
        self._check(subject, limits, estimated_cost)

    def _check(self, subject: str, limits: BudgetLimits, estimated_cost: float):
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
                self._alert(subject, "exceeded", u, limit=name, cap=cap)
                raise BudgetExceeded(name, used, cap, retry)

    def record(self, subject: str, cost_usd: float = 0.0, tokens: int = 0) -> None:
        limits = self.limits_for(subject)
        if limits is None:
            return
        u = self._current(subject, limits)
        u.requests += 1
        u.cost += max(0.0, cost_usd or 0.0)
        u.tokens += max(0, tokens or 0)
        self._warn_check(subject, limits, u)

    def add_usage(self, subject: str, cost_usd: float = 0.0, tokens: int = 0) -> None:
        """Add cost and tokens to a request already counted by ``record``
        (streams only know their usage once they finish)."""
        limits = self.limits_for(subject)
        if limits is None:
            return
        u = self._current(subject, limits)
        u.cost += max(0.0, cost_usd or 0.0)
        u.tokens += max(0, tokens or 0)
        self._warn_check(subject, limits, u)

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
            if (
                isinstance(v, bool)
                or not isinstance(v, (int, float))
                or not math.isfinite(v)
                or v < 0
            ):
                errors.append(f"{where}.{key}: must be a finite number >= 0")
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
    errors = [
        f"unknown top-level key '{k}'"
        for k in data.keys() - {"default", "subjects", "models"}
    ]
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
    models: dict[str, BudgetLimits] = {}
    raw_models = data.get("models") or {}
    if not isinstance(raw_models, dict):
        errors.append("models: must be a mapping")
        raw_models = {}
    for name, raw in raw_models.items():
        # Model caps do not inherit `default`: that one is per subject.
        models[str(name)] = _parse(raw, f"models.{name}", BudgetLimits(), errors)
    if errors:
        raise BudgetConfigError("; ".join(errors))
    return BudgetManager(default, subjects, models, **kwargs)
