import pytest

from localrt.hardware import Hardware
from localrt.provider import ManagedLocalProvider
from localrt.registry import ModelRegistry
from localrt.supervisor import Supervisor
from providers.base import CompletionRequest, Message
from providers.local import LocalProvider
from tests.gguf_helpers import write_gguf
from tests.test_supervisor import FakeProc

GB = 1024**3
KV = {
    "general.architecture": "llama",
    "general.file_type": 15,
    "llama.context_length": 4096,
    "llama.block_count": 32,
    "tokenizer.chat_template": "{{ tools }}",
}


def make(tmp_path, ready_after=0, hw=None, **kw):
    write_gguf(tmp_path / "tiny.gguf", KV)
    calls = {"n": 0}
    ports = iter(range(9000, 9100))

    async def ready(url):
        calls["n"] += 1
        return calls["n"] > ready_after

    sup = Supervisor(spawn=lambda cmd: FakeProc(), port_fn=lambda: next(ports))
    prov = ManagedLocalProvider(
        "gguf",
        ModelRegistry(tmp_path),
        sup,
        binary="llama-server",
        hw=hw or Hardware("cpu", 32 * GB),
        ready=ready,
        load_timeout_s=2,
        **kw,
    )
    return prov, sup, calls


def req(model="tiny"):
    return CompletionRequest(messages=[Message(role="user", content="hi")], model=model)


def test_models_and_capabilities(tmp_path):
    prov, _, _ = make(tmp_path)
    assert prov.models == {"tiny"} and prov.default_model == "tiny"
    assert (
        prov.capabilities("tiny").tools
        and prov.capabilities("tiny").context_length == 4096
    )
    assert prov.capability_problem(req()) is None


@pytest.mark.asyncio
async def test_loads_on_first_request_and_delegates(tmp_path, monkeypatch):
    seen = {}

    async def fake_complete(self, request):
        seen["url"] = self.base_url
        return "ok"

    monkeypatch.setattr(LocalProvider, "complete", fake_complete)
    prov, sup, _ = make(tmp_path)
    assert await prov.complete(req()) == "ok"
    assert seen["url"] == "http://127.0.0.1:9000/v1" and "tiny" in sup.loaded


@pytest.mark.asyncio
async def test_waits_until_ready(tmp_path, monkeypatch):
    async def fake_complete(self, request):
        return "ok"

    monkeypatch.setattr(LocalProvider, "complete", fake_complete)
    prov, _, calls = make(tmp_path, ready_after=2)
    await prov.complete(req())
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_unknown_model_and_no_fit(tmp_path):
    prov, _, _ = make(tmp_path)
    with pytest.raises(ValueError, match="Unknown"):
        await prov.complete(req("nope"))
    small, _, _ = make(tmp_path, hw=Hardware("cpu", 1024))
    with pytest.raises(RuntimeError, match="does not fit"):
        await small.complete(req())


def test_busy_response_via_queue_limit(tmp_path):
    prov, _, _ = make(tmp_path, max_queue=1)
    prov.inflight = 1
    assert "busy" in prov.capability_problem(req())
