"""Declarative routing policies: validation, hot reload, routing, dry run."""

import os

import pytest

from coreai.errors import AllProvidersUnavailable
from coreai.policy import PolicyError, PolicyStore, load_policies, parse_policies
from coreai.router import Router, RoutingConfig, RoutingStrategy
from tests.helpers import FakeClock, StatusError, make_provider, request

GOOD = """
policies:
  default:
    strategy: cost_aware
    weights: {cost: 0.7, latency: 0.3}
    fallback_chain: [a, b, c]
  pinned:
    strategy: explicit
    provider: b
    fallback_chain: [c]
  private:
    strategy: fallback
    allow: [a]
    fallback_chain: [a]
  nob:
    strategy: cheapest
    deny: [b]
"""


def write(path, text):
    path.write_text(text)
    # Make sure mtime changes even on coarse filesystems.
    st = os.stat(path)
    os.utime(path, (st.st_atime, st.st_mtime + 5))


def router_with(tmp_path, text=GOOD, costs=None, **providers_kw):
    f = tmp_path / "routing.yml"
    f.write_text(text)
    costs = costs or {"a": 0.003, "b": 0.001, "c": 0.002}
    providers = {n: make_provider(n, cost=c) for n, c in costs.items()}
    clock = FakeClock()
    store = PolicyStore(str(f), known_providers=providers, clock=clock)
    cfg = RoutingConfig(
        strategy=RoutingStrategy.FALLBACK,
        enable_cache=False,
        retry_initial_wait_seconds=0.001,
        retry_max_wait_seconds=0.01,
    )
    return Router(providers, cfg, clock=clock, policies=store), store, clock, f


# ---- validation ---------------------------------------------------------


def test_valid_policy_loads():
    ps = parse_policies({"policies": {"default": {"strategy": "latency_first"}}})
    assert ps.get(None).strategy == "fastest"


def test_weights_are_normalised():
    ps = parse_policies(
        {"policies": {"default": {"weights": {"cost": 3, "latency": 1}}}}
    )
    p = ps.get("default")
    assert (p.cost_weight, p.latency_weight) == (0.75, 0.25)


@pytest.mark.parametrize(
    "body, fragment",
    [
        ({"strategy": "magic"}, "strategy"),
        ({"strategy": "explicit"}, "requires `provider`"),
        ({"strategy": "fallback"}, "requires `fallback_chain`"),
        ({"weights": {"cost": 0, "latency": 0}}, "both be 0"),
        ({"weights": {"cost": -1, "latency": 1}}, "non-negative"),
        ({"weights": {"speed": 1}}, "only `cost` and `latency`"),
        ({"latency_ceiling_ms": -5}, "positive"),
        ({"fallback_chain": ["a", "a"]}, "duplicate"),
        ({"fallback_chain": "a"}, "list of provider names"),
        ({"allow": ["a"], "deny": ["a"]}, "both allow and deny"),
        ({"bogus": 1}, "unknown key"),
    ],
)
def test_invalid_policies_are_rejected(body, fragment):
    with pytest.raises(PolicyError) as exc:
        parse_policies({"policies": {"p": body}})
    assert fragment in str(exc.value)


def test_unknown_provider_rejected_when_known_set_given():
    with pytest.raises(PolicyError, match="unknown provider 'zzz'"):
        parse_policies(
            {"policies": {"p": {"fallback_chain": ["zzz"]}}}, known_providers=["a"]
        )


def test_all_problems_reported_together():
    with pytest.raises(PolicyError) as exc:
        parse_policies({"policies": {"p": {"strategy": "x", "bogus": 1}, "q": 3}})
    assert len(exc.value.problems) == 3


@pytest.mark.parametrize("doc", [None, [], {}, {"policies": {}}, {"policies": []}])
def test_bad_documents(doc):
    with pytest.raises(PolicyError):
        parse_policies(doc)


def test_bundled_routing_yml_is_valid():
    ps = load_policies("config/routing.yml")
    assert {"default", "low_latency", "high_quality", "bulk"} <= set(ps.names())
    assert ps.get("high_quality").provider == "anthropic"


def test_invalid_yaml_text(tmp_path):
    f = tmp_path / "x.yml"
    f.write_text("policies: [unclosed")
    with pytest.raises(PolicyError, match="invalid YAML"):
        load_policies(str(f))


# ---- hot reload ---------------------------------------------------------


def test_reload_picks_up_changes_and_throttles(tmp_path):
    _, store, clock, f = router_with(tmp_path)
    assert store.reloads == 1
    write(f, GOOD.replace("0.7", "0.9").replace("0.3", "0.1"))
    assert store.current().get("default").cost_weight == pytest.approx(0.7 / 1.0)
    clock.advance(3)
    assert store.current().get("default").cost_weight == pytest.approx(0.9)
    assert store.reloads == 2


def test_bad_edit_keeps_previous_policies(tmp_path):
    _, store, clock, f = router_with(tmp_path)
    write(f, "policies:\n  default:\n    strategy: nonsense\n")
    clock.advance(3)
    assert store.current().get("default").strategy == "balanced"
    assert store.last_error and "nonsense" in store.last_error[0]
    # A later fix clears the error.
    write(f, GOOD)
    os.utime(f, (1, os.stat(f).st_mtime + 10))
    clock.advance(3)
    store.current()
    assert store.last_error is None


def test_missing_file_at_startup_yields_empty_set(tmp_path):
    store = PolicyStore(str(tmp_path / "nope.yml"))
    assert store.current().names() == []
    assert store.last_error


# ---- routing ------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_policy_orders_by_cost_then_chain(tmp_path):
    r, *_ = router_with(tmp_path)
    resp = await r.route(request())
    assert resp.provider == "b"  # cheapest
    assert r._candidates(request(), None) == ["b", "a", "c"]


@pytest.mark.asyncio
async def test_policy_failover_follows_chain(tmp_path):
    r, *_ = router_with(tmp_path)
    r.providers["b"].complete.side_effect = StatusError(503)
    resp = await r.route(request(metadata={"policy": "pinned"}))
    assert resp.provider == "c"


@pytest.mark.asyncio
async def test_explicit_policy_pins_provider_first(tmp_path):
    r, *_ = router_with(tmp_path)
    assert r._candidates(request(metadata={"policy": "pinned"}), None) == ["b", "c"]


@pytest.mark.asyncio
async def test_allow_list_keeps_traffic_off_other_providers(tmp_path):
    r, *_ = router_with(tmp_path)
    r.providers["a"].complete.side_effect = StatusError(503)
    with pytest.raises(StatusError):  # sole allowed provider: original error
        await r.route(request(metadata={"policy": "private"}))
    r.providers["b"].complete.assert_not_called()
    r.providers["c"].complete.assert_not_called()


@pytest.mark.asyncio
async def test_deny_list_excludes_provider(tmp_path):
    r, *_ = router_with(tmp_path)
    assert "b" not in r._candidates(request(metadata={"policy": "nob"}), None)


def test_unknown_policy_name_is_a_value_error(tmp_path):
    r, *_ = router_with(tmp_path)
    with pytest.raises(ValueError, match="Unknown routing policy"):
        r._candidates(request(metadata={"policy": "ghost"}), None)


def test_open_breaker_excludes_provider_from_policy_plan(tmp_path):
    r, *_ = router_with(tmp_path)
    for _ in range(5):
        r.breakers["b"].record_failure()
    assert r._candidates(request(), None) == ["c", "a"]


def test_all_allowed_breakers_open_raises(tmp_path):
    r, *_ = router_with(tmp_path)
    for _ in range(5):
        r.breakers["a"].record_failure()
    with pytest.raises(AllProvidersUnavailable):
        r._candidates(request(metadata={"policy": "private"}), None)


def test_pinned_model_disables_failover_under_policy(tmp_path):
    r, *_ = router_with(tmp_path)
    assert r._candidates(request(model="m"), None) == ["b"]


def test_latency_ceiling_demotes_slow_providers(tmp_path):
    text = """
policies:
  default:
    strategy: cheapest
    latency_ceiling_ms: 200
"""
    r, *_ = router_with(tmp_path, text)
    r.provider_stats["b"]["latency_ms_history"] = [900.0] * 5  # cheapest but slow
    r.provider_stats["a"]["latency_ms_history"] = [100.0] * 5
    r.provider_stats["c"]["latency_ms_history"] = [100.0] * 5
    assert r._candidates(request(), None)[0] == "c"
    assert r._candidates(request(), None)[-1] == "b"


def test_no_store_means_unchanged_behaviour():
    providers = {"a": make_provider("a"), "b": make_provider("b")}
    r = Router(
        providers, RoutingConfig(strategy=RoutingStrategy.FALLBACK, enable_cache=False)
    )
    assert r._candidates(request(metadata={"policy": "ignored"}), None) == ["a", "b"]


# ---- dry run ------------------------------------------------------------


def test_explain_matches_actual_routing_and_has_no_side_effects(tmp_path):
    r, *_ = router_with(tmp_path)
    before = (r.request_count, r._rr_index, dict(r.provider_stats["a"]))
    out = r.explain(request())
    assert out["order"] == r._candidates(request(), None)
    assert out["policy"]["name"] == "default"
    assert {p["provider"] for p in out["providers"]} == {"a", "b", "c"}
    assert all("score" in p for p in out["providers"])
    assert (r.request_count, r._rr_index, r.provider_stats["a"]) == before
    for p in r.providers.values():
        p.complete.assert_not_called()


def test_explain_gives_reasons_for_exclusions(tmp_path):
    r, *_ = router_with(tmp_path)
    for _ in range(5):
        r.breakers["c"].record_failure()
    out = r.explain(request(), policy_name="private")
    by = {p["provider"]: p for p in out["providers"]}
    assert by["a"]["status"] == "eligible"
    assert by["b"]["reason"] == "not in policy allow list"
    assert by["c"]["status"] == "excluded"


def test_explain_with_pinned_provider(tmp_path):
    r, *_ = router_with(tmp_path)
    out = r.explain(request(), preferred_provider="c")
    assert out["order"] == ["c"] and out["pinned_by_request"]
    with pytest.raises(ValueError):
        r.explain(request(), preferred_provider="zzz")


def test_explain_without_policies_does_not_advance_round_robin():
    providers = {n: make_provider(n) for n in "abc"}
    r = Router(
        providers,
        RoutingConfig(strategy=RoutingStrategy.ROUND_ROBIN, enable_cache=False),
    )
    first = r.explain(request())["order"]
    assert r.explain(request())["order"] == first
