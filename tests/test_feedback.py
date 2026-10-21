import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.feedback_api import router
from feedback import FeedbackStore


def test_store_replace_summary_delete():
    s = FeedbackStore()
    s.record("u", "r1", 1)
    s.record("u", "r1", -1, comment="wrong")  # replaces
    s.record("u", "r2", 1)
    assert s.summary("u") == {"up": 1, "down": 1}
    assert s.list("u", rating=-1)[0]["comment"] == "wrong"
    assert s.delete("u", "r1") == 1 and s.delete("u", "r1") == 0


def test_store_validation_and_isolation():
    s = FeedbackStore()
    with pytest.raises(ValueError):
        s.record("u", "r", 0)
    with pytest.raises(ValueError):
        s.record("u", "", 1)
    with pytest.raises(ValueError):
        s.record("u", "r", 1, comment="x" * 9000)
    s.record("a", "r", 1)
    assert s.list("b") == [] and s.summary("b") == {"up": 0, "down": 0}


def test_eval_tasks_need_prompt_and_correction():
    s = FeedbackStore()
    s.record("u", "r1", -1, prompt="Capital of France?", correction="Paris")
    s.record("u", "r2", -1, correction="orphan")
    s.record("u", "r3", 1, prompt="hi")
    t = s.eval_tasks("u")
    assert len(t) == 1
    assert t[0]["scorer"] == {"type": "exact", "expected": "Paris"}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "feedback-test-secret")
    import api.server as server
    from api.auth import create_access_token

    monkeypatch.setitem(server.app_state, "feedback", FeedbackStore())
    app = FastAPI()
    app.include_router(router)

    def h(user, scopes=("feedback:read", "feedback:write")):
        return {"Authorization": "Bearer " + create_access_token(user, list(scopes))}

    return TestClient(app), h


def test_api_flow(env):
    c, h = env
    r = c.post(
        "/v1/feedback",
        json={"request_id": "x", "rating": -1, "prompt": "p", "correction": "c"},
        headers=h("u"),
    )
    assert r.status_code == 201
    out = c.get("/v1/feedback", headers=h("u")).json()
    assert out["summary"]["down"] == 1 and out["data"][0]["request_id"] == "x"
    assert (
        c.get("/v1/feedback/eval-tasks", headers=h("u")).json()["tasks"][0]["prompt"]
        == "p"
    )
    assert c.get("/v1/feedback", headers=h("other")).json()["data"] == []
    assert c.delete("/v1/feedback/x", headers=h("u")).status_code == 200
    assert c.delete("/v1/feedback/x", headers=h("u")).status_code == 404


def test_api_rejects_bad_rating_and_scope(env):
    c, h = env
    assert (
        c.post(
            "/v1/feedback", json={"request_id": "x", "rating": 5}, headers=h("u")
        ).status_code
        == 422
    )
    ro = h("u", ("feedback:read",))
    assert (
        c.post(
            "/v1/feedback", json={"request_id": "x", "rating": 1}, headers=ro
        ).status_code
        == 403
    )
