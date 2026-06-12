"""
CoreAI Protocol Suite - Cost Utilities
Cost calculation, budget tracking, and spend reporting across providers.
"""

from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Optional


# Per-token pricing (USD) by provider + model
# Input/output prices per token (not per 1k — multiply accordingly)
PRICING: dict[str, dict[str, dict[str, float]]] = {
    "openai": {
        "gpt-4o":          {"input": 0.000005,   "output": 0.000015},
        "gpt-4o-mini":     {"input": 0.00000015, "output": 0.0000006},
        "gpt-4-turbo":     {"input": 0.00001,    "output": 0.00003},
        "gpt-3.5-turbo":   {"input": 0.0000005,  "output": 0.0000015},
    },
    "anthropic": {
        "claude-opus-4-5":   {"input": 0.000015,  "output": 0.000075},
        "claude-sonnet-4-5": {"input": 0.000003,  "output": 0.000015},
        "claude-haiku-4-5":  {"input": 0.0000008, "output": 0.000004},
    },
    "gemini": {
        "gemini-2.0-flash":  {"input": 0.0000001,  "output": 0.0000004},
        "gemini-1.5-pro":    {"input": 0.00000125, "output": 0.000005},
        "gemini-1.5-flash":  {"input": 0.000000075,"output": 0.0000003},
    },
    "grok": {
        "grok-2":            {"input": 0.000002, "output": 0.000010},
        "grok-2-mini":       {"input": 0.0000002,"output": 0.000002},
    },
    "deepseek": {
        "deepseek-chat":     {"input": 0.00000027, "output": 0.0000011},
        "deepseek-reasoner": {"input": 0.00000055, "output": 0.00000219},
    },
}

# Fallback pricing when model not in table
DEFAULT_PRICING = {"input": 0.000003, "output": 0.000015}


def calculate_cost(
    input_tokens: int,
    output_tokens: int,
    provider: str,
    model: str,
) -> float:
    """
    Calculate exact cost in USD for a completed request.
    Falls back to provider default, then global default if model unknown.
    """
    provider_pricing = PRICING.get(provider, {})
    model_pricing = provider_pricing.get(model, DEFAULT_PRICING)
    return (
        input_tokens  * model_pricing["input"] +
        output_tokens * model_pricing["output"]
    )


def estimate_cost_for_tokens(
    total_tokens: int,
    provider: str,
    model: str,
    output_ratio: float = 0.4,
) -> float:
    """
    Estimate cost when you only know total tokens (pre-request).
    output_ratio: assumed fraction of tokens that are output (default 40%).
    """
    output_tokens = int(total_tokens * output_ratio)
    input_tokens = total_tokens - output_tokens
    return calculate_cost(input_tokens, output_tokens, provider, model)


def cheapest_provider_for_tokens(
    input_tokens: int,
    output_tokens: int,
    available_providers: list[str],
) -> tuple[str, float]:
    """
    Given token counts, find the cheapest available provider.
    Returns (provider_name, estimated_cost_usd).
    """
    best_provider = available_providers[0]
    best_cost = float("inf")

    for provider in available_providers:
        provider_pricing = PRICING.get(provider, {})
        if not provider_pricing:
            continue
        # Use the cheapest model in that provider's table
        min_cost = min(
            input_tokens * p["input"] + output_tokens * p["output"]
            for p in provider_pricing.values()
        )
        if min_cost < best_cost:
            best_cost = min_cost
            best_provider = provider

    return best_provider, best_cost


def format_cost(cost_usd: float) -> str:
    """Human-readable cost string."""
    if cost_usd == 0:
        return "$0.000000"
    if cost_usd < 0.000001:
        return f"${cost_usd:.8f}"
    if cost_usd < 0.01:
        return f"${cost_usd:.6f}"
    if cost_usd < 1.0:
        return f"${cost_usd:.4f}"
    return f"${cost_usd:.2f}"


# ------------------------------------------------------------------ #
# Budget tracker — per-session spend accumulator
# ------------------------------------------------------------------ #

@dataclass
class BudgetTracker:
    """
    Tracks cumulative spend for a session, agent, or deployment.
    Raises BudgetExceededError when hard limit is hit.
    """
    hard_limit_usd: Optional[float] = None
    warn_at_usd: Optional[float] = None

    _total_cost: float = field(default=0.0, init=False)
    _total_requests: int = field(default=0, init=False)
    _total_input_tokens: int = field(default=0, init=False)
    _total_output_tokens: int = field(default=0, init=False)
    _started_at: datetime = field(default_factory=datetime.utcnow, init=False)
    _warned: bool = field(default=False, init=False)

    def record(
        self,
        cost_usd: float,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ):
        """Record a completed request. Raises if hard limit exceeded."""
        self._total_cost += cost_usd
        self._total_requests += 1
        self._total_input_tokens += input_tokens
        self._total_output_tokens += output_tokens

        if self.warn_at_usd and not self._warned and self._total_cost >= self.warn_at_usd:
            self._warned = True
            # Caller should check warned flag and surface this
        if self.hard_limit_usd and self._total_cost >= self.hard_limit_usd:
            raise BudgetExceededError(
                f"Budget limit of {format_cost(self.hard_limit_usd)} exceeded "
                f"(spent {format_cost(self._total_cost)})"
            )

    def would_exceed(self, estimated_cost: float) -> bool:
        """Check if adding estimated_cost would breach the hard limit."""
        if not self.hard_limit_usd:
            return False
        return (self._total_cost + estimated_cost) > self.hard_limit_usd

    @property
    def total_cost(self) -> float:
        return self._total_cost

    @property
    def is_warned(self) -> bool:
        return self._warned

    def stats(self) -> dict:
        elapsed = (datetime.utcnow() - self._started_at).total_seconds()
        return {
            "total_cost_usd": round(self._total_cost, 6),
            "total_requests": self._total_requests,
            "total_input_tokens": self._total_input_tokens,
            "total_output_tokens": self._total_output_tokens,
            "cost_per_request": round(
                self._total_cost / max(self._total_requests, 1), 6
            ),
            "elapsed_seconds": round(elapsed, 1),
            "hard_limit_usd": self.hard_limit_usd,
            "warn_at_usd": self.warn_at_usd,
            "budget_remaining_usd": (
                round(self.hard_limit_usd - self._total_cost, 6)
                if self.hard_limit_usd else None
            ),
        }


class BudgetExceededError(Exception):
    pass
