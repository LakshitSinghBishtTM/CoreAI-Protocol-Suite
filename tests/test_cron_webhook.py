import hashlib
import hmac
import json
from datetime import datetime, timezone

import pytest

from harness import TriggerManager
from harness.cron import Cron
from harness.runtime import build_runtime
from providers.base import CompletionResponse
from tools import ToolRegistry


def ts(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc).timestamp()


def test_cron_next_matches():
    assert Cron("*/15 * * * *").next_after(ts(2026, 1, 1, 10, 7)) == ts(2026, 1, 1, 10, 15)
    assert Cron("0 9 * * *").next_after(ts(2026, 1, 1, 9, 0)) == ts(2026, 1, 2, 9, 0)
    # 2026-01-05 is a Monday; "0 8 * * 1-5" skips the weekend
    assert Cron("0 8 * * 1-5").next_after(ts(2026, 1, 2, 9)) == ts(2026, 1, 5, 8)
    assert Cron("30 2 29 2 *").next_after(ts(2026, 1, 1)) == ts(2028, 2, 29, 2, 30)
    assert Cron("0 0 1,15 * *").next_after(ts(2026, 1, 2)) == ts(2026, 1, 15)
    assert Cron("0 0 * * 7").next_after(ts(2026, 1, 1)) == ts(2026, 1, 4)  # Sunday


@pytest.mark.parametrize("bad", ["* * * *", "60 * * * *", "*/0 * * * *", "a * * * *", "* * 32 * *", "5-2 * * * *"])
def test_bad_cron_rejected(bad):
    with pytest.raises(ValueError):
        Cron(bad)


async def route(req):
    return CompletionResponse("ok", "m", "p", 1, 1, 0.0, 1.0)


def sign(secret, t, body):
    return hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()


def make(cfg_triggers, monkeypatch=None):
    cfg = {"db": ":memory:", "agents": {"rep": {"system_prompt": "s"}}, "triggers": cfg_triggers}
    return build_runtime(cfg, route, ToolRegistry())


async def test_cron_trigger_fires_on_schedule():
    now = [ts(2026, 1, 1, 10, 0)]
    rt = make({"c": {"cron": "*/5 * * * *", "agent": "rep", "task": "go"}})
    tm = TriggerManager(rt.runner, clock=lambda: now[0])
    tm.cron("c", "*/5 * * * *", "rep", "go")
    now[0] += 60
    assert tm.tick() == []
    now[0] += 240
    assert tm.tick() == ["c"]
    await tm.triggers["c"]._task
    assert tm.triggers["c"].next_at == ts(2026, 1, 1, 10, 10)


async def test_webhook_signature_window_and_fields(monkeypatch):
    monkeypatch.setenv("HOOK_SECRET", "s3")
    rt = make({"h": {"webhook_secret_env": "HOOK_SECRET", "agent": "rep", "task": "alert $level"}})
    tm = rt.triggers
    body = json.dumps({"level": "high"}).encode()
    import time

    now = str(int(time.time()))
    assert tm.fire_webhook("h", body, now, sign("s3", now, body)) is True
    await tm.triggers["h"]._task
    user = [m for m in rt.runner.store.messages(tm.triggers["h"].last_run) if m.role == "user"][0]
    assert user.content == "alert high"
    with pytest.raises(PermissionError):
        tm.fire_webhook("h", body, now, "0" * 64)
    old = str(int(time.time()) - 1000)
    with pytest.raises(PermissionError):
        tm.fire_webhook("h", body, old, sign("s3", old, body))
    with pytest.raises(KeyError):
        tm.fire_webhook("nope", body, now, "x")


def test_webhook_needs_secret_and_one_kind(monkeypatch):
    monkeypatch.delenv("HOOK_SECRET", raising=False)
    with pytest.raises(ValueError, match="secret"):
        make({"h": {"webhook_secret_env": "HOOK_SECRET", "agent": "rep", "task": "x"}})
    with pytest.raises(ValueError, match="exactly one"):
        make({"h": {"cron": "* * * * *", "every_s": 5, "agent": "rep", "task": "x"}})


def test_fire_endpoint_is_401_for_everything_wrong(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import api.server as server
    from api.runs_api import router

    monkeypatch.setenv("HOOK_SECRET", "s3")
    rt = make({"h": {"webhook_secret_env": "HOOK_SECRET", "agent": "rep", "task": "x"}})
    monkeypatch.setitem(server.app_state, "harness", rt)
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as c:
        assert c.post("/v1/triggers/h/fire", content=b"{}").status_code == 401
        assert c.post("/v1/triggers/zzz/fire", content=b"{}").status_code == 401
        import time

        t = str(int(time.time()))
        r = c.post("/v1/triggers/h/fire", content=b"{}",
                   headers={"X-CaiPS-Timestamp": t, "X-CaiPS-Signature": sign("s3", t, b"{}")})  # fmt: skip
        assert r.status_code == 202 and r.json() == {"started": True}
