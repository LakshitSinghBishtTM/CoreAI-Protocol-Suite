"""Price table: arithmetic, validation, unpriced-model handling, shipped file."""

import pytest

from coreai.metrics import render_prometheus
from coreai.pricing import (
    DEFAULT_PATH,
    PriceTable,
    PricingError,
    Rate,
    get_pricing,
    load_pricing,
    set_pricing,
)

YAML = """
version: 1
reviewed: 2026-01-31
providers:
  acme:
    default: {input: 2, output: 8}
    models:
      big: {input: 10, output: 30}
      small: {input: 0.5, output: 1.5}
"""


def write(tmp_path, text):
    p = tmp_path / "pricing.yml"
    p.write_text(text)
    return p


@pytest.fixture
def table(tmp_path):
    return load_pricing(write(tmp_path, YAML))


def test_cost_is_per_million_tokens(table):
    assert table.cost("acme", "big", 1_000_000, 0) == pytest.approx(10)
    assert table.cost("acme", "big", 1000, 500) == pytest.approx(0.025)
    assert table.cost("acme", "small", 0, 0) == 0


def test_negative_or_missing_token_counts_do_not_reduce_cost(table):
    assert table.cost("acme", "big", -5, None) == 0


def test_unlisted_model_uses_default_and_is_counted(table):
    assert table.cost("acme", "new-model", 1_000_000, 0) == pytest.approx(2)
    table.cost("acme", "new-model", 1000, 0)
    assert table.unpriced_stats() == [
        {"provider": "acme", "model": "new-model", "requests": 2}
    ]


def test_unpriced_warning_logged_once_per_model(table):
    from loguru import logger

    seen = []
    sink = logger.add(lambda m: seen.append(str(m)), level="WARNING")
    try:
        for _ in range(5):
            table.cost("acme", "mystery", 1, 1)
    finally:
        logger.remove(sink)
    assert len([m for m in seen if "mystery" in m]) == 1


def test_unpriced_tracking_is_bounded(table, monkeypatch):
    import coreai.pricing as pr

    monkeypatch.setattr(pr, "_MAX_UNPRICED_TRACKED", 3)
    for i in range(20):
        table.cost("acme", f"m{i}", 1, 1)
    models = {u["model"] for u in table.unpriced_stats()}
    assert len(models) == 4 and "other" in models
    assert sum(u["requests"] for u in table.unpriced_stats()) == 20


def test_unknown_provider_is_an_error_not_a_guess(table):
    with pytest.raises(PricingError):
        table.cost("nobody", "x", 1, 1)


def test_reviewed_date_and_listing(table):
    assert table.reviewed == "2026-01-31" or str(table.reviewed) == "2026-01-31"
    assert set(table.models("acme")) == {"big", "small"}


def test_load_reports_all_problems_at_once(tmp_path):
    bad = """
version: 0
extra: 1
providers:
  a:
    models:
      m: {input: -1, output: abc, junk: 1}
  b:
    default: {input: 1, output: 1}
    models: {}
"""
    with pytest.raises(PricingError) as e:
        load_pricing(write(tmp_path, bad))
    msg = str(e.value)
    for needle in (
        "version",
        "extra",
        "providers.a: missing 'default'",
        "m.input",
        "m.output",
        "junk",
        "providers.b.models",
    ):
        assert needle in msg, needle


def test_rejects_non_finite_and_bool_prices(tmp_path):
    for value in (".inf", ".nan", "true"):
        text = f"version: 1\nproviders:\n  a:\n    default: {{input: {value}, output: 1}}\n    models:\n      m: {{input: 1, output: 1}}\n"
        with pytest.raises(PricingError):
            load_pricing(write(tmp_path, text))


def test_missing_file_is_a_clear_error(tmp_path):
    with pytest.raises(PricingError):
        load_pricing(tmp_path / "nope.yml")


def test_output_below_input_is_flagged(table):
    t = PriceTable(
        {
            "a": {
                "default": Rate(1, 1),
                "models": {"odd": Rate(12, 10), "ok": Rate(1, 2)},
            }
        }
    )
    assert t.warnings() == ["a/odd"]


# ------------------------------ shipped file ------------------------------


@pytest.fixture
def shipped():
    return load_pricing(DEFAULT_PATH)


def test_shipped_file_loads_and_covers_every_providers_default_model(shipped):
    from providers.anthropic import AnthropicProvider
    from providers.deepseek import DeepSeekProvider
    from providers.gemini import GeminiProvider
    from providers.grok import GrokProvider
    from providers.openai import OpenAIProvider

    for cls in (
        OpenAIProvider,
        AnthropicProvider,
        GeminiProvider,
        GrokProvider,
        DeepSeekProvider,
    ):
        _, priced = shipped.rate(cls.name, cls.default_model)
        assert priced, f"{cls.name} default model {cls.default_model} has no price"


def test_shipped_prices_match_known_values(shipped):
    """Guards against accidental edits. Update on purpose when prices change."""
    expected = {
        ("openai", "gpt-4o"): (5, 15),
        ("openai", "gpt-4o-mini"): (0.15, 0.6),
        ("anthropic", "claude-sonnet-4-5"): (3, 15),
        ("anthropic", "claude-haiku-4-5"): (0.8, 4),
        ("gemini", "gemini-2.0-flash"): (0.1, 0.4),
        ("grok", "grok-3-mini"): (0.3, 0.5),
        ("deepseek", "deepseek-chat"): (0.27, 1.1),
    }
    for (prov, model), (i, o) in expected.items():
        r, priced = shipped.rate(prov, model)
        assert priced and (r.input_per_mtok, r.output_per_mtok) == (i, o)


def test_shipped_file_known_anomalies(shipped):
    # gemini-2.5-pro has input above output (looks like a typo, flagged in the
    # file). If you verify or fix it, update this list; a NEW anomaly fails here.
    assert shipped.warnings() == ["gemini/gemini-2.5-pro"]
    assert shipped.rate("gemini", "gemini-2.5-pro")[0].note


# --------------------------- facades and surfaces ---------------------------


def test_providers_price_through_the_shared_table():
    from providers.openai import OpenAIProvider

    p = OpenAIProvider.__new__(OpenAIProvider)
    assert p.estimate_cost(1_000_000, 0, "gpt-4o") == pytest.approx(5)
    assert p.estimate_cost(0, 1_000_000, "gpt-4o") == pytest.approx(15)


def test_utils_cost_facade_matches_table():
    from utils.cost import PRICING, calculate_cost, cheapest_provider_for_tokens

    assert calculate_cost(1000, 500, "openai", "gpt-4o") == pytest.approx(0.0125)
    assert PRICING["openai"]["gpt-4o"]["input"] == pytest.approx(0.000005)
    assert calculate_cost(1000, 0, "no-such-provider", "x") == pytest.approx(0.003)
    name, cost = cheapest_provider_for_tokens(1000, 1000, ["openai", "gemini"])
    assert name == "gemini" and cost > 0


def test_unpriced_models_reach_the_metrics(table):
    previous = get_pricing()
    set_pricing(table)
    try:
        table.cost("acme", "ghost", 1, 1)
        from tests.helpers import make_provider, make_router

        router = make_router({"p": make_provider("p")})
        text = render_prometheus(router.stats())
    finally:
        set_pricing(previous)
    assert 'caips_unpriced_requests_total{provider="acme",model="ghost"} 1' in text


# ------------------------------ BudgetTracker ------------------------------


def test_budget_tracker_zero_limits_are_honoured():
    from utils.cost import BudgetExceededError, BudgetTracker

    t = BudgetTracker(hard_limit_usd=0.0)
    assert t.would_exceed(0.0001) is True
    with pytest.raises(BudgetExceededError):
        t.record(0.0)
    w = BudgetTracker(warn_at_usd=0.0)
    w.record(0.0)
    assert w.is_warned


def test_budget_tracker_warns_before_the_hard_limit():
    from utils.cost import BudgetExceededError, BudgetTracker

    t = BudgetTracker(hard_limit_usd=1.0, warn_at_usd=0.5)
    t.record(0.6)
    assert t.is_warned
    with pytest.raises(BudgetExceededError):
        t.record(0.5)
    assert t.stats()["budget_remaining_usd"] == pytest.approx(-0.1)


def test_estimates_do_not_count_as_unpriced_but_billing_does():
    from providers.openai import OpenAIProvider

    previous = get_pricing()
    t = PriceTable(
        {"openai": {"default": Rate(5, 15), "models": {"gpt-4o": Rate(5, 15)}}}
    )
    set_pricing(t)
    try:
        p = OpenAIProvider.__new__(OpenAIProvider)
        assert p.estimate_cost(1000, 0, "brand-new") == pytest.approx(0.005)
        assert t.unpriced_stats() == []  # routing estimate: not counted
        assert p._bill(1000, 0, "brand-new") == pytest.approx(0.005)
        assert t.unpriced_stats()[0]["requests"] == 1
    finally:
        set_pricing(previous)
