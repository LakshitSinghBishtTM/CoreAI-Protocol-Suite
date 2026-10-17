"""Declarative routing policies (``config/routing.yml``).

A policy picks a strategy, optional cost/latency weights, allow and deny
lists and a fallback chain. Files are validated up front: every problem is
reported at once, and a bad edit never replaces a good policy set, so a typo
in a live config cannot take the gateway down.

Only the ``policies`` section is read here; other sections of the file
(``providers``, ``cost_controls`` ...) are ignored.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# YAML strategy name -> canonical name used by the router.
STRATEGIES = {
    "cost_aware": "balanced",  # weighted cost + latency (weights default to cost-heavy)
    "balanced": "balanced",
    "cheapest": "cheapest",
    "latency_first": "fastest",
    "fastest": "fastest",
    "round_robin": "round_robin",
    "fallback": "fallback",  # follow fallback_chain in order
    "explicit": "explicit",  # pin `provider`, fail over along fallback_chain
}

_POLICY_KEYS = {
    "strategy",
    "weights",
    "latency_ceiling_ms",
    "fallback_chain",
    "fallback_model",
    "provider",
    "model",
    "allow",
    "deny",
}
_DEFAULT_COST_AWARE_WEIGHTS = (0.7, 0.3)


class PolicyError(ValueError):
    """The policy file is invalid. ``problems`` lists every issue found."""

    def __init__(self, problems: Iterable[str]):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


@dataclass(frozen=True)
class Policy:
    name: str
    strategy: str  # canonical: balanced|cheapest|fastest|round_robin|fallback|explicit
    cost_weight: float = _DEFAULT_COST_AWARE_WEIGHTS[0]
    latency_weight: float = _DEFAULT_COST_AWARE_WEIGHTS[1]
    latency_ceiling_ms: float | None = None
    fallback_chain: tuple[str, ...] = ()
    fallback_model: str | None = None
    provider: str | None = None
    model: str | None = None
    allow: frozenset[str] | None = None  # None = everything
    deny: frozenset[str] = frozenset()

    def permits(self, provider: str) -> str | None:
        """None if *provider* may serve this policy, else the reason it may not."""
        if provider in self.deny:
            return "denied by policy"
        if self.allow is not None and provider not in self.allow:
            return "not in policy allow list"
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "strategy": self.strategy,
            "weights": {"cost": self.cost_weight, "latency": self.latency_weight},
            "latency_ceiling_ms": self.latency_ceiling_ms,
            "fallback_chain": list(self.fallback_chain),
            "provider": self.provider,
            "model": self.model,
            "allow": sorted(self.allow) if self.allow is not None else None,
            "deny": sorted(self.deny),
        }


@dataclass(frozen=True)
class PolicySet:
    policies: dict[str, Policy] = field(default_factory=dict)

    def get(self, name: str | None) -> Policy | None:
        return self.policies.get(name or "default")

    def names(self) -> list[str]:
        return sorted(self.policies)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _name_list(value: Any, where: str, problems: list[str]) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        problems.append(f"{where}: must be a list of provider names")
        return []
    return list(value)


def parse_policies(
    data: Any, known_providers: Iterable[str] | None = None
) -> PolicySet:
    """Validate a parsed YAML document and build a PolicySet.

    Raises PolicyError listing every problem. With *known_providers*, names
    that aren't registered are errors; without it only structure is checked.
    """
    problems: list[str] = []
    known = set(known_providers) if known_providers is not None else None

    if not isinstance(data, dict):
        raise PolicyError(["top level must be a mapping"])
    raw = data.get("policies")
    if not isinstance(raw, dict) or not raw:
        raise PolicyError(["`policies` must be a non-empty mapping"])

    def check_provider(name: str, where: str) -> None:
        if known is not None and name not in known:
            problems.append(f"{where}: unknown provider {name!r}")

    out: dict[str, Policy] = {}
    for name, body in raw.items():
        where = f"policies.{name}"
        if not isinstance(body, dict):
            problems.append(f"{where}: must be a mapping")
            continue
        for key in sorted(set(body) - _POLICY_KEYS):
            problems.append(f"{where}: unknown key {key!r}")

        strategy_raw = body.get("strategy", "cost_aware")
        strategy = (
            STRATEGIES.get(strategy_raw) if isinstance(strategy_raw, str) else None
        )
        if strategy is None:
            problems.append(
                f"{where}.strategy: {strategy_raw!r} is not one of {sorted(STRATEGIES)}"
            )
            continue

        cost_w, latency_w = _DEFAULT_COST_AWARE_WEIGHTS
        weights = body.get("weights")
        if weights is not None:
            if not isinstance(weights, dict) or set(weights) - {"cost", "latency"}:
                problems.append(f"{where}.weights: only `cost` and `latency` allowed")
            else:
                c = _number(weights.get("cost", 0.0))
                l = _number(weights.get("latency", 0.0))
                if c is None or l is None or c < 0 or l < 0:
                    problems.append(f"{where}.weights: must be non-negative numbers")
                elif c + l == 0:
                    problems.append(
                        f"{where}.weights: cost and latency can't both be 0"
                    )
                else:
                    cost_w, latency_w = c / (c + l), l / (c + l)

        ceiling = body.get("latency_ceiling_ms")
        if ceiling is not None:
            ceiling = _number(ceiling)
            if ceiling is None or ceiling <= 0:
                problems.append(
                    f"{where}.latency_ceiling_ms: must be a positive number"
                )
                ceiling = None

        chain = _name_list(
            body.get("fallback_chain", []), f"{where}.fallback_chain", problems
        )
        if len(set(chain)) != len(chain):
            problems.append(f"{where}.fallback_chain: duplicate providers")
        for p in chain:
            check_provider(p, f"{where}.fallback_chain")

        allow = None
        if "allow" in body:
            allow = frozenset(_name_list(body["allow"], f"{where}.allow", problems))
            for p in allow:
                check_provider(p, f"{where}.allow")
        deny = frozenset(_name_list(body.get("deny", []), f"{where}.deny", problems))
        for p in deny:
            check_provider(p, f"{where}.deny")
        if allow is not None and allow & deny:
            problems.append(
                f"{where}: {sorted(allow & deny)} are in both allow and deny"
            )

        provider = body.get("provider")
        if provider is not None:
            if not isinstance(provider, str):
                problems.append(f"{where}.provider: must be a string")
                provider = None
            else:
                check_provider(provider, f"{where}.provider")
        if strategy == "explicit" and provider is None:
            problems.append(f"{where}: strategy `explicit` requires `provider`")
        if strategy == "fallback" and not chain:
            problems.append(f"{where}: strategy `fallback` requires `fallback_chain`")

        model = body.get("model")
        fallback_model = body.get("fallback_model")
        for key, val in (("model", model), ("fallback_model", fallback_model)):
            if val is not None and not isinstance(val, str):
                problems.append(f"{where}.{key}: must be a string")

        out[str(name)] = Policy(
            name=str(name),
            strategy=strategy,
            cost_weight=cost_w,
            latency_weight=latency_w,
            latency_ceiling_ms=ceiling,
            fallback_chain=tuple(chain),
            fallback_model=fallback_model if isinstance(fallback_model, str) else None,
            provider=provider,
            model=model if isinstance(model, str) else None,
            allow=allow,
            deny=deny,
        )

    if problems:
        raise PolicyError(problems)
    return PolicySet(out)


def load_policies(path: str, known_providers: Iterable[str] | None = None) -> PolicySet:
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise PolicyError([f"{path}: invalid YAML ({exc.__class__.__name__})"]) from exc
    except OSError as exc:
        raise PolicyError([f"{path}: cannot read file ({exc.strerror})"]) from exc
    return parse_policies(data, known_providers)


class PolicyStore:
    """Holds the active PolicySet and reloads it when the file changes.

    ``current()`` is cheap: it stats the file at most once per
    ``check_interval`` seconds. A file that fails validation is logged and the
    previous policies stay active.
    """

    def __init__(
        self,
        path: str,
        known_providers: Iterable[str] | None = None,
        check_interval: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.path = path
        self._known = list(known_providers) if known_providers is not None else None
        self._interval = check_interval
        self._clock = clock
        self._mtime: float | None = None
        self._next_check = 0.0
        self.last_error: list[str] | None = None
        self.reloads = 0
        self._set = PolicySet()
        self._load(initial=True)
        self._next_check = self._clock() + self._interval

    def current(self) -> PolicySet:
        now = self._clock()
        if now >= self._next_check:
            self._next_check = now + self._interval
            self._load()
        return self._set

    def _load(self, initial: bool = False) -> None:
        try:
            mtime = os.stat(self.path).st_mtime
        except OSError:
            if initial:
                self.last_error = [f"{self.path}: file not found"]
                logger.error("Routing policy file missing: %s", self.path)
            return
        if mtime == self._mtime:
            return
        self._mtime = mtime
        try:
            self._set = load_policies(self.path, self._known)
        except PolicyError as exc:
            self.last_error = exc.problems
            logger.error(
                "Routing policy reload rejected, keeping previous policies: %s", exc
            )
            return
        self.last_error = None
        self.reloads += 1
        logger.info("Loaded routing policies: %s", ", ".join(self._set.names()))
