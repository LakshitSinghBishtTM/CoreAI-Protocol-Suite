"""Usage ledger: spend and traffic over time, for reports.

Records are rolled into fixed time buckets per (subject, provider, model), so
memory is bounded by ``retention`` and the number of distinct keys, not by
request volume. Reports can be grouped and exported as JSON or CSV.

Behaviour notes:
- In memory and per process; a restart empties it. Use the Prometheus
  metrics for long-term history.
- Buckets older than ``retention_seconds`` are dropped; the number of live
  rows is capped at ``max_rows`` (oldest dropped first).
"""

from __future__ import annotations

import csv
import io
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

BUCKETS = {"hour": 3600, "day": 86400}
GROUP_BY = ("subject", "provider", "model")
COLUMNS = (
    "period_start",
    "subject",
    "provider",
    "model",
    "requests",
    "input_tokens",
    "output_tokens",
    "cost_usd",
)


class UsageQueryError(ValueError):
    pass


@dataclass
class _Row:
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


class UsageLedger:
    def __init__(
        self,
        retention_seconds: int = 30 * 86400,
        max_rows: int = 50_000,
        clock: Callable[[], float] = time.time,
    ):
        if (
            isinstance(retention_seconds, bool)
            or not isinstance(retention_seconds, int)
            or retention_seconds < 1
        ):
            raise ValueError("retention_seconds must be a positive integer")
        if isinstance(max_rows, bool) or not isinstance(max_rows, int) or max_rows < 1:
            raise ValueError("max_rows must be a positive integer")
        self.retention_seconds = retention_seconds
        self.max_rows = max_rows
        self._clock = clock
        # (hour_start, subject, provider, model) -> _Row, oldest first
        self._rows: OrderedDict[tuple, _Row] = OrderedDict()

    def _prune(self) -> None:
        cutoff = self._clock() - self.retention_seconds
        while self._rows:
            key = next(iter(self._rows))
            if key[0] < cutoff or len(self._rows) > self.max_rows:
                self._rows.popitem(last=False)
            else:
                break

    def record(
        self,
        subject: str,
        provider: str = "",
        model: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float = 0.0,
        requests: int = 1,
    ) -> None:
        now = self._clock()
        hour = now - (now % 3600)
        key = (hour, subject, provider or "", model or "")
        row = self._rows.get(key)
        if row is None:
            row = _Row()
            # Retention pruning removes from the oldest end. Wall-clock time
            # can move backwards, so restore hour ordering when a new bucket
            # arrives earlier than the current newest bucket.
            if self._rows and hour < next(reversed(self._rows))[0]:
                self._rows = OrderedDict(
                    sorted(
                        [*self._rows.items(), (key, row)], key=lambda item: item[0][0]
                    )
                )
            else:
                self._rows[key] = row
        row.requests += requests
        row.input_tokens += max(0, input_tokens or 0)
        row.output_tokens += max(0, output_tokens or 0)
        row.cost_usd += max(0.0, cost_usd or 0.0)
        self._prune()

    def report(
        self,
        subject: str | None = None,
        since: float | None = None,
        until: float | None = None,
        bucket: str = "day",
        group_by: tuple[str, ...] | list[str] = (),
    ) -> list[dict]:
        """Rows sorted by period then group. ``subject`` filters to one
        subject (``None`` = all). ``since`` is inclusive, ``until`` exclusive."""
        if bucket not in BUCKETS:
            raise UsageQueryError(f"bucket must be one of {sorted(BUCKETS)}")
        bad = [g for g in group_by if g not in GROUP_BY]
        if bad:
            raise UsageQueryError(f"group_by must be from {list(GROUP_BY)}: {bad}")
        if since is not None and until is not None and since >= until:
            raise UsageQueryError("since must be before until")
        # Usage is stored in hourly buckets, so a bound inside an hour cannot be
        # honoured exactly. Refuse it rather than silently include or drop usage.
        for label, bound in (("since", since), ("until", until)):
            if bound is not None and bound % BUCKETS["hour"] != 0:
                raise UsageQueryError(
                    f"{label} must be aligned to a whole hour (a multiple of 3600 "
                    "seconds); usage is recorded in hourly buckets"
                )
        size = BUCKETS[bucket]
        agg: dict[tuple, _Row] = {}
        for (hour, subj, prov, model), row in self._rows.items():
            if subject is not None and subj != subject:
                continue
            if since is not None and hour < since:
                continue
            if until is not None and hour >= until:
                continue
            period = hour - (hour % size)
            dims = {"subject": subj, "provider": prov, "model": model}
            key = (period, *(dims[g] for g in group_by))
            a = agg.setdefault(key, _Row())
            a.requests += row.requests
            a.input_tokens += row.input_tokens
            a.output_tokens += row.output_tokens
            a.cost_usd += row.cost_usd
        out = []
        for key in sorted(agg):
            a = agg[key]
            item: dict = {"period_start": int(key[0])}
            for g, v in zip(group_by, key[1:]):
                item[g] = v
            item.update(
                requests=a.requests,
                input_tokens=a.input_tokens,
                output_tokens=a.output_tokens,
                cost_usd=round(a.cost_usd, 6),
            )
            out.append(item)
        return out


def to_csv(rows: list[dict], group_by: tuple[str, ...] | list[str] = ()) -> str:
    """CSV with a header row; only the columns present in the report."""
    cols = [c for c in COLUMNS if c in ("period_start", *group_by) or c not in GROUP_BY]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow({c: _safe(r.get(c, "")) for c in cols})
    return buf.getvalue()


def _safe(value):
    """Neutralise spreadsheet formula injection in text cells (model and
    subject names come from clients)."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value
