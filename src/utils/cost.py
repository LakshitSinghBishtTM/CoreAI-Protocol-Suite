"""
CoreAI Protocol Suite - Cost Utilities
Cost calculation, budget tracking, and spend reporting across providers.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from coreai.pricing import get_pricing

# Legacy view of the shared price table (config/pricing.yml): USD per single
# token, by provider and model. Prefer coreai.pricing.get_pricing().
PRICING: dict[str, dict[str, dict[str, float]]] = get_pricing().per_token()

# Used only for a provider that has no table at all (USD per token).
DEFAULT_PRICING = {"input": 0.000003, "output": 0.000015}


def calculate_cost(
    input_tokens: int,
    output_tokens: int,
    provider: str,
    model: str,
) -> float:
    """
    Cost in USD for a completed request, from config/pricing.yml.
    An unlisted model uses its provider's default rate (and is counted as
    unpriced); an unknown provider uses DEFAULT_PRICING.
    """
    table = get_pricing()
    rate, _ = table.rate(provider, model)
    if rate is None:
        return (
            input_tokens * DEFAULT_PRICING["input"]
            + output_tokens * DEFAULT_PRICING["output"]
        )
    return table.cost(provider, model, input_tokens, output_tokens)


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

    table = get_pricing()
    for provider in available_providers:
        rates = table.models(provider)
        if not rates:
            continue
        # Use the cheapest model in that provider's table
        min_cost = min(r.cost(input_tokens, output_tokens) for r in rates.values())
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

        if (
            self.warn_at_usd is not None
            and not self._warned
            and self._total_cost >= self.warn_at_usd
        ):
            self._warned = True
            # Caller should check warned flag and surface this
        if self.hard_limit_usd is not None and self._total_cost >= self.hard_limit_usd:
            raise BudgetExceededError(
                f"Budget limit of {format_cost(self.hard_limit_usd)} exceeded "
                f"(spent {format_cost(self._total_cost)})"
            )

    def would_exceed(self, estimated_cost: float) -> bool:
        """Check if adding estimated_cost would breach the hard limit."""
        if self.hard_limit_usd is None:
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
                if self.hard_limit_usd is not None
                else None
            ),
        }


class BudgetExceededError(Exception):
    pass
