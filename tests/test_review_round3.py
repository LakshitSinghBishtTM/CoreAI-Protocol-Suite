"""Regression tests for the third code review (routing, lifecycle, accounting)."""

import asyncio
import math
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from agents.task_orchestrator import TaskOrchestrator
from api.middleware import RateLimitMiddleware
from coreai.budgets import BudgetConfigError, BudgetExceeded, load_budgets
from coreai.circuit_breaker import CircuitBreaker, CircuitState
from coreai.router import Router, RoutingConfig, RoutingStrategy
from coreai.usage import UsageLedger, UsageQueryError
from neural.model import DEFAULT_MODELS, get_registry
from providers.base import CompletionRequest, Message, normalize_finish_reason
from tests.helpers import FakeClock, make_provider


def _req(**kw):
    return CompletionRequest(messages=[Message(role="user", content="hi")], **kw)


def _router(strategy=RoutingStrategy.CHEAPEST, **providers):
    cfg = RoutingConfig(strategy=strategy, enable_cache=False)
    return Router(providers, cfg)


def test_explicit_model_selects_owning_provider():
    r = _router(
        deepseek=make_provider("deepseek", cost=0.0001),
        anthropic=make_provider("anthropic", cost=0.01),
    )
    assert r._candidates(_req(model="claude-3-5-haiku-20241022"), None) == ["anthropic"]


def test_model_provider_mismatch_is_rejected():
    r = _router(
        deepseek=make_provider("deepseek"), anthropic=make_provider("anthropic")
    )
    with pytest.raises(ValueError):
        r._candidates(_req(model="claude-3-5-haiku-20241022"), "deepseek")


def test_output_limit_and_streaming_flag_are_enforced():
    r = _router(openai=make_provider("openai"))
    with pytest.raises(ValueError, match="output limit"):
        asyncio.run(r.route(_req(model="gpt-4o", max_tokens=20_000)))
    assert "streaming" in r._model_problem(_req(model="o3-mini", stream=True), "openai")


def test_registry_defaults_match_adapters():
    reg = get_registry()
    assert DEFAULT_MODELS["anthropic"] == "claude-haiku-4-5"
    assert DEFAULT_MODELS["gemini"] == "gemini-2.0-flash"
    assert DEFAULT_MODELS["grok"] == "grok-3-mini"
    for prov in ("anthropic", "gemini", "grok"):
        assert reg.default_for(prov).provider == prov


def test_model_cap_on_one_candidate_fails_over():
    r = _router(
        strategy=RoutingStrategy.FALLBACK,
        a=make_provider("a"),
        b=make_provider("b"),
    )

    def gate(model):
        if model == "a-model":
            raise BudgetExceeded("max_requests", 1, 1, 10.0)

    r.model_gate = gate
    resp = asyncio.run(r.route(_req()))
    assert resp.provider == "b"


def test_model_cap_applies_to_cache_hits():
    cfg = RoutingConfig(strategy=RoutingStrategy.FALLBACK, enable_cache=True)
    r = Router({"a": make_provider("a")}, cfg)
    asyncio.run(r.route(_req()))  # populate cache
    r.model_gate = lambda m: (_ for _ in ()).throw(
        BudgetExceeded("max_requests", 1, 1, 10.0)
    )
    with pytest.raises(BudgetExceeded):
        asyncio.run(r.route(_req()))


def test_cost_ranking_uses_requested_output_size():
    r = _router(a=make_provider("a"))
    seen = []
    r.providers["a"].estimate_cost.side_effect = (
        lambda i, o, m: seen.append((i, o)) or 0.0
    )
    r._select_cheapest(_req(max_tokens=700), ["a"])
    assert seen[0][1] == 700


def test_breaker_late_success_does_not_close_open_circuit():
    clock = FakeClock()
    b = CircuitBreaker("x", failure_threshold=2, reset_timeout=30, clock=clock)
    assert b.allow() and b.allow() and b.allow()
    b.record_failure()
    b.record_failure()
    assert b.state is CircuitState.OPEN
    b.record_success()  # stale in-flight success
    assert b.state is CircuitState.OPEN
    clock.advance(31)
    assert b.allow()  # half-open probe
    b.record_success()
    assert b.state is CircuitState.CLOSED


def test_rate_limit_key_uses_full_api_key_and_idle_clients_are_swept():
    mw = RateLimitMiddleware.__new__(RateLimitMiddleware)
    mw.rpm = 60
    mw._windows = {}

    def req(k):
        return SimpleNamespace(
            headers={"X-API-Key": k}, client=SimpleNamespace(host="h")
        )  # noqa: E731

    k1 = mw._get_client_key(req("sharedprefix-0000-aaaa"))
    k2 = mw._get_client_key(req("sharedprefix-0000-bbbb"))
    assert k1 != k2
    mw._check_rate_limit("stale")
    mw._windows["stale"].clear()
    mw._windows["stale2"] = __import__("collections").deque([1.0])
    mw._last_sweep = 0.0
    mw._check_rate_limit("fresh")
    assert "stale" not in mw._windows and "stale2" not in mw._windows


def test_usage_report_rejects_unaligned_bounds():
    led = UsageLedger(clock=lambda: 7200.0)
    led.record("s")
    assert led.report(since=3600, until=10800)
    with pytest.raises(UsageQueryError):
        led.report(since=3601)
    with pytest.raises(UsageQueryError):
        led.report(until=5000)


def test_non_finite_budget_values_are_rejected(tmp_path):
    f = tmp_path / "b.yml"
    f.write_text("default:\n  max_cost_usd: .nan\n")
    with pytest.raises(BudgetConfigError):
        load_budgets(str(f))
    assert not math.isfinite(float("inf"))


def test_finish_reason_normalisation():
    assert normalize_finish_reason("max_tokens") == "length"
    assert normalize_finish_reason("end_turn") == "stop"
    assert normalize_finish_reason("SAFETY") == "content_filter"
    assert normalize_finish_reason(None) is None


def test_cancelling_a_running_legacy_task_stops_the_work():
    started = asyncio.Event()
    finished = []

    class Mgr:
        def get_idle_agents(self):
            return ["a1"]

        async def dispatch_task(self, agent_id, payload):
            started.set()
            await asyncio.sleep(5)
            finished.append(1)

    async def run():
        orch = TaskOrchestrator(Mgr())
        await orch.start()
        tid = await orch.submit("do it")
        await asyncio.wait_for(started.wait(), 3)
        assert orch.cancel(tid)
        await asyncio.sleep(0.05)
        assert orch.get_status(tid)["status"] == "cancelled"
        await orch.stop()

    asyncio.run(run())
    assert not finished


def test_expired_api_key_is_rejected(monkeypatch):
    import contextlib

    import api.auth as auth
    import database.db as db

    rec = SimpleNamespace(
        owner="o",
        id="1",
        scopes="x",
        expires_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1),
    )

    class Sess:
        async def execute(self, *_a, **_k):
            return SimpleNamespace(scalar_one_or_none=lambda: rec)

    @contextlib.asynccontextmanager
    async def fake_session():
        yield Sess()

    monkeypatch.setattr(db, "get_session", fake_session)
    assert asyncio.run(auth._validate_api_key_against_db("k")) is None
    rec.expires_at = None
    assert asyncio.run(auth._validate_api_key_against_db("k")) is not None


def _policy_router(tmp_path, text, **providers):
    from coreai.policy import PolicyStore

    f = tmp_path / "routing.yml"
    f.write_text(text)
    cfg = RoutingConfig(strategy=RoutingStrategy.FALLBACK, enable_cache=False)
    store = PolicyStore(str(f), known_providers=providers)
    return Router(providers, cfg, policies=store)


def _p(name):
    return make_provider(name)


def test_policy_model_and_fallback_model_are_applied(tmp_path):
    r = _policy_router(
        tmp_path,
        "policies:\n  default:\n    strategy: fallback\n    fallback_chain: [openai, anthropic]\n"
        "    model: gpt-4o\n    fallback_model: claude-3-5-haiku-20241022\n",
        openai=_p("openai"),
        anthropic=_p("anthropic"),
    )
    names, models = r._plan(_req(), None)
    assert names == ["openai", "anthropic"]
    assert models == ["gpt-4o", "claude-3-5-haiku-20241022"]
    assert r.policies.current().get("default").to_dict()["fallback_model"]


def test_round_robin_policy_rotates(tmp_path):
    r = _policy_router(
        tmp_path,
        "policies:\n  default:\n    strategy: round_robin\n",
        a=_p("a"),
        b=_p("b"),
        c=_p("c"),
    )
    firsts = [r._plan(_req(), None)[0][0] for _ in range(3)]
    assert sorted(firsts) == ["a", "b", "c"]
    # explain() must not advance the rotation
    before = dict(r._policy_rr)
    r.explain(_req())
    assert r._policy_rr == before


def test_explicit_policy_without_chain_does_not_fail_over(tmp_path):
    r = _policy_router(
        tmp_path,
        "policies:\n  default:\n    strategy: explicit\n    provider: a\n",
        a=_p("a"),
        b=_p("b"),
    )
    assert r._plan(_req(), None)[0] == ["a"]


def test_named_policy_forwarded_by_api_schema():
    from api.routes import CompletionRequest as Api

    assert (
        Api(messages=[{"role": "user", "content": "x"}], policy="bulk").policy == "bulk"
    )
    with pytest.raises(Exception):
        Api(messages=[])
    with pytest.raises(Exception):
        Api(messages=[{"role": "tool", "content": "x"}])


def test_virtual_model_alias_maps_to_policy(tmp_path):
    r = _policy_router(
        tmp_path,
        "policies:\n  default:\n    strategy: fallback\n    fallback_chain: [a, b]\n"
        "  low_latency:\n    strategy: fallback\n    fallback_chain: [b, a]\n",
        a=_p("a"),
        b=_p("b"),
    )
    req = _req(model="fast")
    resp = asyncio.run(r.route(req))
    assert resp.provider == "b" and req.model is None
    # an alias whose policy doesn't exist is left alone; real models aren't shadowed
    req2 = _req(model="cheap")
    r.resolve_alias(req2)
    assert req2.model == "cheap"
    req3 = _req(model="gpt-4o")
    r.resolve_alias(req3)
    assert req3.model == "gpt-4o"


def test_embeddings_route_to_supporting_provider():
    from unittest.mock import AsyncMock

    plain = make_provider("a")
    plain.supports_embeddings = False
    emb = make_provider("openai")
    emb.supports_embeddings = True
    emb.embed = AsyncMock(return_value=([[0.1, 0.2]], 3, "text-embedding-3-small"))
    r = _router(a=plain, openai=emb)
    vectors, tokens, model, provider = asyncio.run(r.embed(["hi"]))
    assert provider == "openai" and tokens == 3 and vectors == [[0.1, 0.2]]
    with pytest.raises(ValueError):
        asyncio.run(_router(a=plain).embed(["hi"]))
