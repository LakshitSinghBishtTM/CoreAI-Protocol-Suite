"""Guardrail policy applied on /v1/chat/completions (config/guardrails.yml)."""

import pytest

from guardrails import config as gcfg
from tests.helpers import make_provider, make_router
from tests.test_openai_compat import make_client  # noqa: F401  (fixture)


def body(text):
    return {"model": "a-model", "messages": [{"role": "user", "content": text}]}


@pytest.fixture
def policy(tmp_path, monkeypatch):
    f = tmp_path / "g.yml"
    f.write_text(
        "input:\n  blocked_topics: [forbidden]\n"
        "output:\n  block_secrets: true\n  redact_pii: true\n"
    )
    monkeypatch.setenv("COREAI_GUARDRAILS_CONFIG", str(f))
    gcfg.reset()
    yield
    gcfg.reset()


def test_no_config_means_no_filtering(make_client, tmp_path, monkeypatch):
    monkeypatch.setenv("COREAI_GUARDRAILS_CONFIG", str(tmp_path / "missing.yml"))
    gcfg.reset()
    c = make_client(make_router({"a": make_provider("a")}))
    assert (
        c.post("/v1/chat/completions", json=body("forbidden bob@x.io")).status_code
        == 200
    )


def test_blocked_input_is_rejected_before_any_provider(make_client, policy):
    p = make_provider("a")
    c = make_client(make_router({"a": p}))
    r = c.post("/v1/chat/completions", json=body("something Forbidden here"))
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "content_filter"
    assert not p.complete.called


def test_pii_in_input_is_redacted_before_the_provider_sees_it(make_client, policy):
    p = make_provider("a")
    c = make_client(make_router({"a": p}))
    assert (
        c.post("/v1/chat/completions", json=body("mail bob@example.com")).status_code
        == 200
    )
    sent = p.complete.call_args[0][0]
    assert "bob@example.com" not in str(sent.messages[-1].content)
