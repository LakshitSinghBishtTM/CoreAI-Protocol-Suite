"""Per-provider health score from recent outcomes.

The score is the time-decayed success rate over a bounded window, from 0.0
(everything failing) to 1.0 (healthy or no data). Recent outcomes weigh more
than old ones, so a provider that recovers is trusted again within a few
half-lives. One pseudo-success is always included, so a single early failure
doesn't zero a provider that has barely been used.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable


class HealthTracker:
    def __init__(
        self,
        half_life_seconds: float = 60.0,
        max_events: int = 200,
        clock: Callable[[], float] = time.monotonic,
    ):
        if half_life_seconds <= 0:
            raise ValueError("half_life_seconds must be positive")
        self._half_life = half_life_seconds
        self._clock = clock
        self._events: deque[tuple[float, bool]] = deque(maxlen=max_events)

    def record(self, ok: bool) -> None:
        self._events.append((self._clock(), ok))

    def score(self) -> float:
        now = self._clock()
        good = 1.0  # prior: one fresh success
        total = 1.0
        for t, ok in self._events:
            w = 0.5 ** (max(0.0, now - t) / self._half_life)
            total += w
            if ok:
                good += w
        return good / total

    def sample_count(self) -> int:
        return len(self._events)
