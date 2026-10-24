"""Per-subject budgets: limits, windows, config loading and the HTTP surface."""

import pytest
from fastapi.testclient import TestClient

from coreai.budgets import (
    BudgetConfigError,
    BudgetExceeded,
    BudgetLimits,
    BudgetManager,
    load_budgets,
)
from tests.helpers import FakeClock, make_provider, make_router

BODY = {"messages": [{"role": "user", "content": "hi"}]}


def manager(clock=None, **limits):
    return BudgetManager(BudgetLimits(**limits), clock=clock or FakeClock(0.0))


@pytest.mark.parametrize(
    "limits",
    [
        {"window_seconds": 0},
        {"window_seconds": 1.5},
        {"max_cost_usd": -1},
        {"max_cost_usd": float("nan")},
        {"warn_cost_usd": float("inf")},
        {"max_requests": -1},
        {"max_tokens": 1.5},
        {"max_cost_usd": 1, "warn_cost_usd": 2},
    ],
)
def test_direct_budget_limits_reject_invalid_values(limits):
    with pytest.raises(ValueError):
        BudgetLimits(**limits)


def test_direct_budget_limits_keep_zero_as_a_valid_hard_cap():
    m = manager(max_requests=0)
    with pytest.raises(BudgetExceeded):
        m.check("u")


@pytest.mark.parametrize("max_subjects", [0, -1, 1.5, True])
def test_budget_manager_requires_positive_subject_capacity(max_subjects):
    with pytest.raises(ValueError, match="max_subjects must be a positive integer"):
        BudgetManager(max_subjects=max_subjects)


def test_no_limits_means_no_checks():
    m = BudgetManager(clock=FakeClock())
    m.check("u")
    m.record("u", 5.0, 100)
    assert m.status("u") is None


def test_cost_limit_blocks_after_spend():
    m = manager(max_cost_usd=1.0)
    m.check("u")
    m.record("u", cost_usd=1.0)
    with pytest.raises(BudgetExceeded) as e:
        m.check("u")
    assert e.value.limit == "cost_usd"


def test_request_and_token_limits():
    m = manager(max_requests=2)
    for _ in range(2):
        m.check("u")
        m.record("u")
    with pytest.raises(BudgetExceeded) as e:
        m.check("u")
    assert e.value.limit == "requests"

    t = manager(max_tokens=100)
    t.record("u", tokens=100)
    with pytest.raises(BudgetExceeded) as e:
        t.check("u")
    assert e.value.limit == "tokens"


def test_zero_limit_blocks_everything():
    for field in ("max_cost_usd", "max_requests", "max_tokens"):
        with pytest.raises(BudgetExceeded):
            manager(**{field: 0}).check("u")


def test_subjects_are_independent():
    m = manager(max_requests=1)
    m.record("a")
    m.check("b")
    with pytest.raises(BudgetExceeded):
        m.check("a")


def test_window_rolls_over_and_retry_after_points_at_reset():
    clock = FakeClock(100.0)
    m = manager(clock, max_requests=1, window_seconds=60)
    m.record("u")
    with pytest.raises(BudgetExceeded) as e:
        m.check("u")
    assert e.value.retry_after == pytest.approx(20.0)  # window 60..120, now 100
    clock.advance(21)
    m.check("u")
    assert m.status("u")["used"]["requests"] == 0


def test_estimated_cost_counts_before_the_call():
    m = manager(max_cost_usd=1.0)
    m.record("u", cost_usd=0.9)
    m.check("u")
    with pytest.raises(BudgetExceeded):
        m.check("u", estimated_cost=0.2)


def test_soft_limit_sets_warning_without_blocking():
    m = manager(max_cost_usd=1.0, warn_cost_usd=0.5)
    m.record("u", cost_usd=0.6)
    m.check("u")
    assert m.status("u")["warning"] is True


def test_tracked_subjects_are_bounded():
    m = BudgetManager(BudgetLimits(max_requests=5), clock=FakeClock(), max_subjects=3)
    for i in range(20):
        m.record(f"u{i}")
    assert m.stats()["tracked_subjects"] == 3


def write(tmp_path, text):
    p = tmp_path / "budgets.yml"
    p.write_text(text)
    return str(p)


def test_load_with_default_and_subject_override(tmp_path):
    m = load_budgets(
        write(
            tmp_path,
            "default: {max_cost_usd: 5, window: hour}\n"
            "subjects:\n  'user:vip': {max_cost_usd: 50}\n",
        )
    )
    assert m.limits_for("anyone").max_cost_usd == 5
    vip = m.limits_for("user:vip")
    assert vip.max_cost_usd == 50 and vip.window_seconds == 3600  # inherited


def test_load_reports_all_problems_at_once(tmp_path):
    with pytest.raises(BudgetConfigError) as e:
        load_budgets(
            write(
                tmp_path,
                "default: {max_cost_usd: -1, window: fortnight, bogus: 1}\nextra: 1\n",
            )
        )
    msg = str(e.value)
    assert "max_cost_usd" in msg and "window" in msg
    assert "bogus" in msg and "extra" in msg


def test_warn_above_max_is_rejected(tmp_path):
    with pytest.raises(BudgetConfigError):
        load_budgets(write(tmp_path, "default: {max_cost_usd: 1, warn_cost_usd: 2}\n"))


# ---------------------------- HTTP surface ----------------------------


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "budget-test-secret")
    from unittest.mock import MagicMock, patch

    from fastapi import FastAPI

    import api.server as server
    from api.auth import create_access_token
    from api.routes import router as api_router

    app = FastAPI()
    app.include_router(api_router)
    orch = MagicMock()
    orch.stats.return_value = {}
    budgets = BudgetManager(BudgetLimits(max_requests=2), clock=FakeClock(0.0))
    router = make_router({"p": make_provider("p")})
    patches = [
        patch.object(server, "get_router", return_value=router),
        patch.object(server, "get_orchestrator", return_value=orch),
        patch.object(server, "get_budgets", return_value=budgets),
    ]
    for p in patches:
        p.start()
    token = create_access_token("user:t", ["completions:read", "completions:write"])
    client = TestClient(app)
    client.headers.update({"Authorization": f"Bearer {token}"})
    yield client, budgets
    for p in patches:
        p.stop()


def test_over_budget_gets_429_with_retry_after(api):
    client, _ = api
    assert client.post("/v1/completions", json=BODY).status_code == 200
    assert client.post("/v1/completions", json=BODY).status_code == 200
    r = client.post("/v1/completions", json=BODY)
    assert r.status_code == 429
    assert r.json()["detail"]["error"] == "budget_exceeded"
    assert int(r.headers["Retry-After"]) >= 1


def test_usage_endpoint_reports_consumption(api):
    client, _ = api
    client.post("/v1/completions", json=BODY)
    body = client.get("/v1/usage").json()
    assert body["budget"]["used"]["requests"] == 1
    assert body["budget"]["limits"]["requests"] == 2


def test_failed_provider_call_is_not_billed(api):
    client, budgets = api
    import api.server as server
    from tests.helpers import StatusError

    bad = make_router({"p": make_provider("p", behaviour=StatusError(400))})
    server.get_router.return_value = bad
    assert client.post("/v1/completions", json=BODY).status_code == 400
    assert budgets.status("user:t")["used"]["requests"] == 0


# --- per-model caps -------------------------------------------------------


def test_model_cap_is_shared_across_subjects():
    m = BudgetManager(
        models={"big": BudgetLimits(max_cost_usd=1.0)}, clock=FakeClock(0.0)
    )
    m.check_model("big")
    m.record_model("big", cost_usd=0.6)
    m.record_model("big", cost_usd=0.6)  # another caller's call; same cap
    with pytest.raises(BudgetExceeded) as e:
        m.check_model("big")
    assert e.value.limit == "cost_usd"
    m.check_model("small")  # uncapped and unset models are never blocked
    m.check_model(None)


def test_model_cap_does_not_collide_with_subject_names(tmp_path):
    m = BudgetManager(
        BudgetLimits(max_requests=1),
        models={"x": BudgetLimits(max_requests=5)},
        clock=FakeClock(0.0),
    )
    m.record_model("x")
    m.record("model:x")
    m.check_model("x")


def test_models_section_loads_and_validates(tmp_path):
    from coreai.budgets import BudgetConfigError, load_budgets

    f = tmp_path / "b.yml"
    f.write_text("models:\n  gpt:\n    max_cost_usd: 2\n")
    assert load_budgets(str(f)).models["gpt"].max_cost_usd == 2.0
    f.write_text("models:\n  gpt:\n    max_cost_usd: -1\n    bogus: 1\n")
    with pytest.raises(BudgetConfigError) as e:
        load_budgets(str(f))
    assert "models.gpt" in str(e.value) and "bogus" in str(e.value)


def test_model_cap_enforced_over_http(api):
    client, budgets = api
    budgets.models["p-model"] = BudgetLimits(max_requests=1)
    body = {**BODY, "model": "p-model"}
    assert client.post("/v1/completions", json=body).status_code == 200
    r = client.post("/v1/completions", json=body)
    assert r.status_code == 429
    assert r.json()["detail"]["limit"] == "requests"
    # auto routing onto the capped model is enforced too (no ``model`` sent)
    assert client.post("/v1/completions", json=BODY).status_code == 429
    # a request naming a different model is unaffected
    other = {**BODY, "model": "other-model"}
    assert client.post("/v1/completions", json=other).status_code == 200
