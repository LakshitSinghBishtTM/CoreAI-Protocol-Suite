"""harness/cron.py: five-field cron expressions (UTC), no dependencies.

``minute hour day-of-month month day-of-week``; supports ``*``, lists (``1,15``),
ranges (``9-17``) and steps (``*/5``, ``0-30/10``). Day-of-week is 0-6 with 0 =
Sunday (7 also means Sunday). As in classic cron, when both day-of-month and
day-of-week are restricted, a day matches if either does. No names (``mon``),
no ``@daily`` shortcuts, no seconds.
"""

from datetime import datetime, timedelta, timezone

_RANGES = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]


def _field(text: str, lo: int, hi: int) -> set[int]:
    out: set[int] = set()
    for part in text.split(","):
        rng, _, step = part.partition("/")
        step_n = int(step) if step else 1
        if step_n < 1:
            raise ValueError(f"bad step in {part!r}")
        if rng == "*":
            a, b = lo, hi
        elif "-" in rng:
            a, b = (int(x) for x in rng.split("-", 1))
        else:
            a = int(rng)
            b = hi if step else a
        if not (lo <= a <= b <= hi):
            raise ValueError(f"{part!r} outside {lo}-{hi}")
        out.update(range(a, b + 1, step_n))
    return out


class Cron:
    def __init__(self, expr: str):
        parts = expr.split()
        if len(parts) != 5:
            raise ValueError("cron needs 5 fields: minute hour dom month dow")
        try:
            sets = [_field(p, lo, hi) for p, (lo, hi) in zip(parts, _RANGES)]
        except ValueError as exc:
            raise ValueError(f"bad cron {expr!r}: {exc}") from None
        self.minute, self.hour, self.dom, self.month, dow = sets
        self.dow = {d % 7 for d in dow}
        self.dom_any, self.dow_any = parts[2] == "*", parts[4] == "*"
        self.expr = expr

    def _day_ok(self, d: datetime) -> bool:
        in_dom, in_dow = d.day in self.dom, (d.isoweekday() % 7) in self.dow
        if self.dom_any or self.dow_any:
            return in_dom and in_dow
        return in_dom or in_dow

    def next_after(self, ts: float) -> float:
        """First matching minute strictly after ``ts`` (epoch seconds)."""
        d = datetime.fromtimestamp(ts, timezone.utc).replace(second=0, microsecond=0)
        d += timedelta(minutes=1)
        for _ in range(366 * 24 * 60 * 5):  # five years of minutes is plenty
            if d.month not in self.month:
                d = (d.replace(day=1, hour=0, minute=0) + timedelta(days=32)).replace(day=1)
            elif not self._day_ok(d):
                d = d.replace(hour=0, minute=0) + timedelta(days=1)
            elif d.hour not in self.hour:
                d = d.replace(minute=0) + timedelta(hours=1)
            elif d.minute not in self.minute:
                d += timedelta(minutes=1)
            else:
                return d.timestamp()
        raise ValueError(f"cron {self.expr!r} never matches")
