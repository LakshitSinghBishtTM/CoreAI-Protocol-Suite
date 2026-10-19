"""
coreai/circuit_breaker.py

Per-provider circuit breaker.

  CLOSED     normal operation; consecutive failures are counted
  OPEN       too many failures; calls are refused until the reset timeout passes
  HALF_OPEN  after the timeout, a limited number of probe calls are allowed;
             one success closes the circuit, one failure reopens it

The clock is injectable so tests can advance time without sleeping.
"""

import time
from enum import Enum
from typing import Callable, Optional


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        reset_timeout: float = 30.0,
        half_open_max_calls: int = 1,
        clock: Callable[[], float] = time.monotonic,
    ):
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be at least 1")
        self.name = name
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self.half_open_max_calls = half_open_max_calls
        self._clock = clock

        self._state = CircuitState.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._probes_in_flight = 0
        self.total_opened = 0

    @property
    def state(self) -> CircuitState:
        if (
            self._state is CircuitState.OPEN
            and self._clock() - self._opened_at >= self.reset_timeout
        ):
            self._state = CircuitState.HALF_OPEN
            self._probes_in_flight = 0
        return self._state

    def would_allow(self) -> bool:
        """Peek: would a call be admitted right now? No side effects."""
        state = self.state
        if state is CircuitState.CLOSED:
            return True
        if state is CircuitState.HALF_OPEN:
            return self._probes_in_flight < self.half_open_max_calls
        return False

    def allow(self) -> bool:
        """Admit a call. Every True must be followed by record_success/failure."""
        state = self.state
        if state is CircuitState.CLOSED:
            return True
        if state is CircuitState.HALF_OPEN:
            if self._probes_in_flight < self.half_open_max_calls:
                self._probes_in_flight += 1
                return True
        return False

    def record_success(self) -> None:
        state = self.state
        if state is CircuitState.OPEN:
            # A late success from a call admitted before the circuit opened
            # says nothing about recovery; only a half-open probe may close it.
            return
        if state is CircuitState.HALF_OPEN:
            self._probes_in_flight = max(0, self._probes_in_flight - 1)
        self._state = CircuitState.CLOSED
        self._failures = 0

    def record_failure(self) -> None:
        state = self.state
        if state is CircuitState.OPEN:
            return  # already open; late failures must not extend the window
        if state is CircuitState.HALF_OPEN:
            self._probes_in_flight = max(0, self._probes_in_flight - 1)
            self._open()
            return
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._open()

    def release(self) -> None:
        """Give back a half-open probe slot without recording an outcome
        (for calls that ended for reasons unrelated to provider health)."""
        if self._state is CircuitState.HALF_OPEN:
            self._probes_in_flight = max(0, self._probes_in_flight - 1)

    def retry_after(self) -> Optional[float]:
        """Seconds until the breaker may admit a probe (None if not open)."""
        if self._state is not CircuitState.OPEN:
            return None
        return max(0.0, self.reset_timeout - (self._clock() - self._opened_at))

    def _open(self) -> None:
        self._state = CircuitState.OPEN
        self._opened_at = self._clock()
        self._probes_in_flight = 0
        self.total_opened += 1

    def snapshot(self) -> dict:
        return {
            "state": self.state.value,
            "consecutive_failures": self._failures,
            "times_opened": self.total_opened,
            "retry_after_seconds": self.retry_after(),
        }
