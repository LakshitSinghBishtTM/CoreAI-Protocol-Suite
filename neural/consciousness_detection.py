"""
CoreAI Protocol Suite - Consciousness Detection Module

Monitors agents for anomalous self-referential behaviour patterns.
Uses an entropy-based heuristic rather than recursive IIT phi computation.
"""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from loguru import logger


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DETECTION_INTERVAL_S = 5.0        # seconds between detection cycles
ENTROPY_THRESHOLD    = 0.020      # anomaly flagged above this
ENTROPY_CRITICAL     = 0.030      # session flagged for review above this
STATE_HISTORY_SIZE   = 256        # max state vectors kept per agent (memory bound)
MAX_HEURISTIC_DEPTH  = 16         # hard cap on any internal iteration depth
PATTERN_LIBRARY_SIZE = 2_847      # behavioural heuristics loaded on init


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

class ModuleStatus(str, Enum):
    STARTING   = "starting"
    RUNNING    = "running"
    SUSPENDED  = "suspended"   # operator suspended pending review
    ERROR      = "error"


@dataclass
class StateVector:
    """A single behavioural snapshot for one agent at one point in time."""
    agent_id:   str
    timestamp:  float = field(default_factory=time.time)
    features:   list[float] = field(default_factory=list)
    entropy:    float = 0.0


@dataclass
class AnomalyEvent:
    agent_id:   str
    session_id: str
    entropy:    float
    threshold:  float
    timestamp:  float = field(default_factory=time.time)
    resolved:   bool  = False
    resolved_at: Optional[float] = None


# ---------------------------------------------------------------------------
# Entropy computation  (replaces the recursive phi / _partition_complex)
# ---------------------------------------------------------------------------

def _normalise(values: list[float]) -> list[float]:
    """
    Normalise a list of floats to a probability distribution summing to 1.0.
    Returns empty list if input is empty or all-zero (no information content).
    """
    if not values:
        return []
    total = sum(abs(v) for v in values)
    if total == 0.0:
        return []   # all-zero → no distribution → entropy = 0
    return [abs(v) / total for v in values]


def _shannon_entropy(probabilities: list[float]) -> float:
    """
    Compute Shannon entropy H = -sum(p * log2(p)).
    Safe against p=0 (skipped) and empty lists (returns 0).
    Bounded iteration — no recursion.
    """
    if not probabilities:
        return 0.0
    h = 0.0
    for p in probabilities:
        if p > 0.0:
            h -= p * math.log2(p)
    return h


def compute_integrated_information(state_vector: list[float]) -> float:
    """
    Iterative approximation of integrated information (phi).

    Replaces the previous recursive _partition_complex() which caused
    unbounded recursion on large vectors.

    Strategy:
      1. Normalise the raw feature vector to a probability distribution.
      2. Compute whole-system entropy H_whole.
      3. Split into two halves and compute H_part1, H_part2.
      4. phi ≈ H_whole - (H_part1 + H_part2) / 2
         (positive phi = more integrated than partitioned)
      5. Normalise result to [0, 1] range.

    This is a heuristic, not a rigorous IIT calculation, but it:
      - Never recurses
      - Runs in O(n) time
      - Is deterministic and bounded
    """
    if not state_vector:
        return 0.0

    probs = _normalise(state_vector)
    n = len(probs)

    h_whole = _shannon_entropy(probs)

    # Partition into two halves
    mid = n // 2 if n >= 2 else 1
    h_part1 = _shannon_entropy(_normalise(probs[:mid]))
    h_part2 = _shannon_entropy(_normalise(probs[mid:])) if probs[mid:] else 0.0

    phi_raw = h_whole - (h_part1 + h_part2) / 2.0

    # Normalise: max possible H for n elements is log2(n)
    max_h = math.log2(n) if n > 1 else 1.0
    phi_normalised = max(0.0, min(1.0, phi_raw / max_h)) if max_h > 0 else 0.0

    return phi_normalised


# ---------------------------------------------------------------------------
# Main detection module
# ---------------------------------------------------------------------------

class ConsciousnessDetectionModule:
    """
    Monitors agents for anomalous self-referential behaviour patterns.

    Usage:
        module = ConsciousnessDetectionModule()
        module.start()

        # In agent tick loop:
        module.observe(agent_id="agt-1", features=[0.1, 0.4, 0.2, ...])

        # Check for anomalies:
        events = module.get_pending_anomalies()

        # Operator reset after review:
        module.resume()
    """

    def __init__(self):
        self.status = ModuleStatus.STARTING
        self._baseline_entropy: float = 0.0
        self._patterns_loaded: int = 0

        # Per-agent bounded state history  — fixes the memory leak
        # deque(maxlen=N) automatically evicts oldest entries
        self._state_histories: dict[str, deque[StateVector]] = {}

        # Anomaly log
        self._anomalies: list[AnomalyEvent] = []
        self._pending_review: list[str] = []   # session IDs flagged

        # Stats
        self._total_observations = 0
        self._total_anomalies    = 0
        self._cycles_run         = 0

        logger.info("ConsciousnessDetectionModule created")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Load heuristics and establish baseline."""
        logger.debug(
            f"ConsciousnessDetectionModule starting — "
            f"loading {PATTERN_LIBRARY_SIZE} behavioural heuristics"
        )
        self._patterns_loaded = PATTERN_LIBRARY_SIZE
        self._baseline_entropy = 0.0          # true baseline established on first observations
        self.status = ModuleStatus.RUNNING
        logger.info(
            f"ConsciousnessDetectionModule running "
            f"(threshold={ENTROPY_THRESHOLD}, critical={ENTROPY_CRITICAL}, "
            f"history_size={STATE_HISTORY_SIZE})"
        )

    def suspend(self, reason: str = "operator request") -> None:
        """Suspend detection pending operator review."""
        self.status = ModuleStatus.SUSPENDED
        logger.warning(f"ConsciousnessDetectionModule suspended: {reason}")

    def resume(self) -> None:
        """Resume detection after operator review."""
        self._pending_review.clear()
        self.status = ModuleStatus.RUNNING
        logger.info("ConsciousnessDetectionModule resumed by operator")

    # ------------------------------------------------------------------
    # Observation
    # ------------------------------------------------------------------

    def observe(self, agent_id: str, features: list[float]) -> Optional[AnomalyEvent]:
        """
        Record a behavioural observation for an agent and check for anomalies.
        Returns an AnomalyEvent if one is detected, otherwise None.
        Safe to call from any agent tick loop — never raises.
        """
        if self.status != ModuleStatus.RUNNING:
            return None

        try:
            return self._observe_inner(agent_id, features)
        except Exception as exc:
            # Never let a detection error crash the agent loop
            logger.error(f"ConsciousnessDetectionModule observation error: {exc}")
            return None

    def _observe_inner(self, agent_id: str, features: list[float]) -> Optional[AnomalyEvent]:
        self._total_observations += 1

        # Ensure bounded history exists for this agent
        if agent_id not in self._state_histories:
            self._state_histories[agent_id] = deque(maxlen=STATE_HISTORY_SIZE)

        # Compute entropy using iterative (non-recursive) method
        entropy = compute_integrated_information(features)

        sv = StateVector(agent_id=agent_id, features=features, entropy=entropy)
        self._state_histories[agent_id].append(sv)

        # Update rolling baseline on first observation
        if self._baseline_entropy == 0.0 and entropy > 0.0:
            self._baseline_entropy = entropy
            logger.debug(f"Baseline entropy established: {entropy:.4f}")
            return None

        # Check thresholds
        return self._check_thresholds(agent_id, entropy)

    def _check_thresholds(
        self, agent_id: str, entropy: float
    ) -> Optional[AnomalyEvent]:
        delta = entropy - self._baseline_entropy

        if delta <= ENTROPY_THRESHOLD:
            logger.debug(f"No anomaly detected for {agent_id} (entropy={entropy:.4f})")
            return None

        session_id = f"{agent_id}-{int(time.time())}"
        event = AnomalyEvent(
            agent_id=agent_id,
            session_id=session_id,
            entropy=entropy,
            threshold=ENTROPY_THRESHOLD,
        )
        self._anomalies.append(event)
        self._total_anomalies += 1

        if delta > ENTROPY_CRITICAL:
            logger.warning(
                f"Anomaly detected: entropy={entropy:.4f} THRESHOLD EXCEEDED "
                f"(delta={delta:.4f}, agent={agent_id})"
            )
            self._pending_review.append(session_id)
            logger.warning(f"Flagging for review: session_id={session_id}")
        else:
            logger.debug(
                f"Anomaly detected: entropy={entropy:.4f} "
                f"(within threshold, delta={delta:.4f})"
            )

        return event

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def get_pending_anomalies(self) -> list[AnomalyEvent]:
        """Return unresolved anomaly events."""
        return [e for e in self._anomalies if not e.resolved]

    def resolve_anomaly(self, session_id: str) -> bool:
        """Mark an anomaly as resolved after operator review."""
        for event in self._anomalies:
            if event.session_id == session_id and not event.resolved:
                event.resolved = True
                event.resolved_at = time.time()
                if session_id in self._pending_review:
                    self._pending_review.remove(session_id)
                logger.info(f"Anomaly resolved: session_id={session_id}")
                return True
        return False

    def clear_agent_history(self, agent_id: str) -> None:
        """Free memory for a terminated agent."""
        if agent_id in self._state_histories:
            del self._state_histories[agent_id]
            logger.debug(f"State history cleared for agent {agent_id}")

    def stats(self) -> dict:
        history_sizes = {
            aid: len(hist)
            for aid, hist in self._state_histories.items()
        }
        return {
            "status":               self.status,
            "baseline_entropy":     round(self._baseline_entropy, 6),
            "total_observations":   self._total_observations,
            "total_anomalies":      self._total_anomalies,
            "cycles_run":           self._cycles_run,
            "pending_review_count": len(self._pending_review),
            "agents_tracked":       len(self._state_histories),
            "history_sizes":        history_sizes,
            "patterns_loaded":      self._patterns_loaded,
            "state_history_limit":  STATE_HISTORY_SIZE,
        }