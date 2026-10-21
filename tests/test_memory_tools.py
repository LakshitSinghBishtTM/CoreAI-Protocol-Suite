import pytest

from memory import MemoryStore
from memory.tool import memory_tools
from tools import Permission, ToolRegistry
from tools.config import build_runtime


def make():
    store = MemoryStore()
    r = ToolRegistry()
    for t in memory_tools(store, "alice"):
        r.register(t)
    return store, r


async def test_remember_search_forget_roundtrip():
    store, r = make()
    names = ["memory_remember", "memory_search", "memory_forget"]
    await r.call(
        "memory_remember",
        {"key": "pet", "value": "cat named Miso"},
        allowed=names,
        approved=False,
    )
    assert store.recall("alice", "pet") == "cat named Miso"
    out = await r.call("memory_search", {"query": "cat"}, allowed=names, approved=False)
    assert "Miso" in str(out.content if hasattr(out, "content") else out)
    assert "untrusted" in str(out.content if hasattr(out, "content") else out).lower()


def test_permissions_levels():
    _, r = make()
    assert r.get("memory_search").permission == Permission.READ_ONLY
    assert r.get("memory_remember").permission == Permission.WRITE
    assert r.get("memory_forget").permission == Permission.DESTRUCTIVE


async def test_owner_cannot_be_overridden_by_arguments():
    from tools.registry import ToolError

    store, r = make()
    store.remember("bob", "secret", "bob's data")
    with pytest.raises(ToolError) as e:
        await r.call(
            "memory_search", {"query": "data", "owner": "bob"},
            allowed=["memory_search"], approved=False,
        )  # fmt: skip
    assert e.value.code == "invalid_arguments"
    out = await r.call(
        "memory_search", {"query": "data"}, allowed=["memory_search"], approved=False
    )
    assert "bob's data" not in str(getattr(out, "content", out))


def test_config_registers_memory_tools(tmp_path):
    rt = build_runtime({"memory": {"owner": "u", "db": str(tmp_path / "m.db")}})
    assert rt.registry.get("memory_search") is not None
    with pytest.raises(ValueError):
        build_runtime({"memory": {"db": ":memory:"}})
