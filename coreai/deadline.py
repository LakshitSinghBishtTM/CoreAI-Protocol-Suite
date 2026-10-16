"""
coreai/deadline.py

A request-wide time budget. One Deadline is created per request and shared by
every retry and failover attempt, so a slow first provider cannot consume the
whole budget and leave nothing for the second.
"""

import time
from typing import Callable, Optional

from .errors import DeadlineExceeded


class Deadline:
    def __init__(
        self,
        seconds: Optional[float],
        clock: Callable[[], float] = time.monotonic,
    ):
        self._clock = clock
        self._end = None if seconds is None else clock() + seconds

    def remaining(self) -> Optional[float]:
        """Seconds left, or None for an unlimited deadline."""
        if self._end is None:
            return None
        return max(0.0, self._end - self._clock())

    def expired(self) -> bool:
        return self._end is not None and self._clock() >= self._end

    def check(self) -> None:
        if self.expired():
            raise DeadlineExceeded("request deadline exceeded")

    def clamp(self, seconds: Optional[float]) -> Optional[float]:
        """The smaller of *seconds* and the time remaining (None = unlimited)."""
        remaining = self.remaining()
        if remaining is None:
            return seconds
        if seconds is None:
            return remaining
        return min(seconds, remaining)
