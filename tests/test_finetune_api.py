import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from neural.finetune import TrainingPrices
from neural.training import TrainingManager
from tests.test_finetune import FakeClient

PRICES = TrainingPrices({"gpt-4o-mini": 3.0})
EXAMPLES = [
    {
        "messages": [
            {"role": "user", "content": "q" * 400},
            {"role": "assistant", "content": "a" * 400},
        ]
    }
    for _ in range(12)
]


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "ft-api-test-secret")
    import api.server as server
    import neural.training as t
    from api.auth import create_access_token
    from api.routes import router

    client = FakeClient({"status": "queued"})
    prov = types.SimpleNamespace(fine_tuning_client=lambda: client)
    monkeypatch.setattr(
        server, "get_router", lambda: types.SimpleNamespace(providers={"openai": prov})
    )
    monkeypatch.setattr(t, "_manager", TrainingManager(prices=PRICES))
    app = FastAPI()
    app.include_router(router)

    def h(scopes=("admin",)):
        return {"Authorization": "Bearer " + create_access_token("u", list(scopes))}

    return TestClient(app), h, client


def body(**kw):
    return {"base_model": "gpt-4o-mini", "examples": EXAMPLES, **kw}


def test_estimate_does_not_submit(env):
    c, h, client = env
    r = c.post("/v1/finetune/estimate", json=body(), headers=h())
    assert r.status_code == 200
    d = r.json()
    assert (
        d["validation"]["valid"]
        and d["price_per_mtok"] == 3.0
        and d["estimated_usd"] > 0
    )
    assert client.created == [] and c.get("/v1/finetune/jobs", headers=h()).json() == []


def test_estimate_reports_invalid_dataset(env):
    c, h, _ = env
    bad = body(examples=[{"messages": [{"role": "assistant", "content": "x"}]}])
    d = c.post("/v1/finetune/estimate", json=bad, headers=h()).json()
    assert d["validation"]["valid"] is False


def test_cost_limit_returns_422_and_uploads_nothing(env):
    c, h, client = env
    est = c.post("/v1/finetune/estimate", json=body(), headers=h()).json()[
        "estimated_usd"
    ]
    r = c.post("/v1/finetune/jobs", json=body(max_cost_usd=est / 2), headers=h())
    assert r.status_code == 422 and "exceeds the limit" in r.json()["detail"]
    assert client.created == []


def test_submit_within_limit_reports_estimate(env):
    c, h, client = env
    est = c.post("/v1/finetune/estimate", json=body(), headers=h()).json()[
        "estimated_usd"
    ]
    r = c.post("/v1/finetune/jobs", json=body(max_cost_usd=est + 1), headers=h())
    assert (
        r.status_code == 201
        and r.json()["estimated_usd"] == est
        and len(client.created) == 1
    )


def test_admin_only_and_unknown_preset(env):
    c, h, _ = env
    assert (
        c.post(
            "/v1/finetune/estimate", json=body(), headers=h(["completions:read"])
        ).status_code
        == 403
    )
    assert (
        c.post(
            "/v1/finetune/estimate", json=body(hyperparam_preset="wild"), headers=h()
        ).status_code
        == 422
    )
