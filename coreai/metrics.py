"""Prometheus text exposition built from ``Router.stats()``.

No client library: the gateway already keeps the counters, so this only
renders them. Everything here is a pure function of the stats dict.
"""

from __future__ import annotations

from collections.abc import Iterable

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
_STATES = ("closed", "open", "half_open")


def _esc(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _labels(**labels: object) -> str:
    if not labels:
        return ""
    body = ",".join(f'{k}="{_esc(v)}"' for k, v in labels.items())
    return "{" + body + "}"


class _Writer:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def metric(
        self,
        name: str,
        kind: str,
        help_: str,
        samples: Iterable[tuple[dict, float]],
    ) -> None:
        samples = list(samples)
        if not samples:
            return
        self.lines.append(f"# HELP {name} {help_}")
        self.lines.append(f"# TYPE {name} {kind}")
        for labels, value in samples:
            self.lines.append(f"{name}{_labels(**labels)} {float(value):g}")


def _histogram(w: "_Writer", stats: dict) -> None:
    bounds = stats.get("latency_bucket_bounds_ms") or []
    rows = stats.get("model_stats") or []
    if not rows or not bounds:
        return
    name = "caips_request_duration_seconds"
    w.lines.append(f"# HELP {name} Provider call latency of successful requests.")
    w.lines.append(f"# TYPE {name} histogram")
    for r in rows:
        base = {"provider": r["provider"], "model": r["model"]}
        running = 0
        for bound, count in zip(bounds, r["latency_buckets"]):
            running += count
            le = f"{bound / 1000:g}"
            w.lines.append(f"{name}_bucket{_labels(**base, le=le)} {running}")
        running += r["latency_buckets"][len(bounds)]
        w.lines.append(f"{name}_bucket{_labels(**base, le='+Inf')} {running}")
        w.lines.append(f"{name}_sum{_labels(**base)} {r['latency_sum_ms'] / 1000:g}")
        w.lines.append(f"{name}_count{_labels(**base)} {running}")


def render_prometheus(stats: dict, *, accepting: bool = True, in_flight: int = 0):
    """Render router stats as Prometheus text format."""
    providers = stats.get("provider_stats") or {}
    w = _Writer()

    w.metric(
        "caips_requests_total",
        "counter",
        "Requests handled by the gateway.",
        [({}, stats.get("total_requests", 0))],
    )
    w.metric(
        "caips_failovers_total",
        "counter",
        "Requests that moved to another provider after a failure.",
        [({}, stats.get("failovers", 0))],
    )
    hedges = stats.get("hedges") or {}
    w.metric(
        "caips_hedges_launched_total",
        "counter",
        "Hedged requests launched.",
        [({}, hedges.get("launched", 0))],
    )
    w.metric(
        "caips_hedges_won_total",
        "counter",
        "Hedged requests where the second provider answered first.",
        [({}, hedges.get("won_by_second", 0))],
    )
    w.metric(
        "caips_provider_requests_total",
        "counter",
        "Requests sent to a provider.",
        [({"provider": n}, s.get("requests", 0)) for n, s in providers.items()],
    )
    w.metric(
        "caips_provider_errors_total",
        "counter",
        "Failed provider calls by error kind.",
        [
            ({"provider": n, "kind": kind}, count)
            for n, s in providers.items()
            for kind, count in sorted((s.get("error_kinds") or {}).items())
        ],
    )
    w.metric(
        "caips_provider_cost_usd_total",
        "counter",
        "Estimated spend per provider in USD.",
        [({"provider": n}, s.get("total_cost") or 0) for n, s in providers.items()],
    )
    w.metric(
        "caips_provider_latency_p50_ms",
        "gauge",
        "Median recent latency per provider in milliseconds.",
        [
            ({"provider": n}, s["p50_latency_ms"])
            for n, s in providers.items()
            if s.get("p50_latency_ms") is not None
        ],
    )
    w.metric(
        "caips_provider_health",
        "gauge",
        "Recent success rate per provider, 0 to 1.",
        [
            ({"provider": n}, s["health"])
            for n, s in providers.items()
            if s.get("health") is not None
        ],
    )
    w.metric(
        "caips_circuit_state",
        "gauge",
        "1 for the circuit breaker's current state per provider, else 0.",
        [
            ({"provider": n, "state": st}, int(_state(s) == st))
            for n, s in providers.items()
            for st in _STATES
        ],
    )
    w.metric(
        "caips_circuit_opened_total",
        "counter",
        "Times a provider's circuit breaker has opened.",
        [
            ({"provider": n}, (s.get("circuit") or {}).get("times_opened", 0))
            for n, s in providers.items()
        ],
    )

    _histogram(w, stats)
    w.metric(
        "caips_tokens_total",
        "counter",
        "Tokens processed, by provider, model and direction.",
        [
            ({"provider": r["provider"], "model": r["model"], "type": t}, r[f"{t}_tokens"])
            for r in stats.get("model_stats") or []
            for t in ("input", "output")
        ],
    )

    w.metric(
        "caips_unpriced_requests_total",
        "counter",
        "Requests for models missing from config/pricing.yml (default rate used).",
        [
            ({"provider": u["provider"], "model": u["model"]}, u["requests"])
            for u in stats.get("unpriced_models") or []
        ],
    )

    cache = stats.get("cache_stats") or {}
    for key, name, help_ in (
        ("hits", "caips_cache_hits_total", "Response cache hits."),
        ("misses", "caips_cache_misses_total", "Response cache misses."),
    ):
        if isinstance(cache.get(key), (int, float)):
            w.metric(name, "counter", help_, [({}, cache[key])])

    w.metric(
        "caips_accepting_traffic",
        "gauge",
        "1 while the gateway accepts new requests, 0 while draining.",
        [({}, int(accepting))],
    )
    w.metric(
        "caips_in_flight_requests",
        "gauge",
        "Requests currently being served.",
        [({}, in_flight)],
    )
    return "\n".join(w.lines) + "\n"


def _state(provider_stats: dict) -> str:
    return (provider_stats.get("circuit") or {}).get("state", "closed")
