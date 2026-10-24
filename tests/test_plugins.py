import pytest

import providers
from providers.base import BaseProvider
from tools.config import build_runtime
from tools.registry import Tool, ToolRegistry
from utils import plugins


class EP:
    def __init__(self, name, obj=None, error=None):
        self.name, self._obj, self._error = name, obj, error

    def load(self):
        if self._error:
            raise self._error
        return self._obj


@pytest.fixture
def fake_eps(monkeypatch):
    table = {}
    monkeypatch.setattr(plugins, "entry_points", lambda group: table.get(group, []))
    monkeypatch.delenv("CAIPS_PLUGINS", raising=False)
    return table


def tool(name, **kw):
    return Tool(name, "d", {"type": "object", "properties": {}}, lambda: "ok", **kw)


def test_nothing_loads_without_allow_list(fake_eps):
    fake_eps[plugins.TOOL_GROUP] = [EP("p", lambda: [tool("x")])]
    reg = ToolRegistry()
    assert plugins.register_tool_plugins(reg) == []
    assert reg.get("x") is None


def test_env_allow_list_and_owner_tag(fake_eps, monkeypatch):
    fake_eps[plugins.TOOL_GROUP] = [
        EP("p", lambda: [tool("x")]),
        EP("other", lambda: [tool("y")]),
    ]
    monkeypatch.setenv("CAIPS_PLUGINS", "p")
    reg = ToolRegistry()
    assert plugins.register_tool_plugins(reg) == ["x"]
    assert reg.get("x").owner == "plugin:p" and reg.get("y") is None


def test_broken_and_clashing_plugins_are_skipped(fake_eps):
    def boom():
        raise RuntimeError("bad")

    fake_eps[plugins.TOOL_GROUP] = [
        EP("broken", error=ImportError("nope")),
        EP("boom", boom),
        EP("good", lambda: [tool("dup"), tool("fine")]),
    ]
    reg = ToolRegistry()
    reg.register(tool("dup"))
    added = plugins.register_tool_plugins(reg, ["broken", "boom", "good"])
    assert added == ["fine"] and reg.get("dup").owner == "builtin"


def test_provider_plugin_registered_but_builtins_protected(fake_eps):
    class P(BaseProvider):
        name = "plug"
        env_var = "PLUG_KEY"

    class NoEnv(BaseProvider):
        name = "noenv"

    fake_eps[plugins.PROVIDER_GROUP] = [
        EP("plug", P),
        EP("openai", P),
        EP("noenv", NoEnv),
    ]
    pmap = {"openai": (object, "OPENAI_API_KEY")}
    assert plugins.register_provider_plugins(pmap, ["plug", "openai", "noenv"]) == [
        "plug"
    ]
    assert pmap["plug"] == (P, "PLUG_KEY") and pmap["openai"][0] is object


def test_load_providers_uses_plugin(fake_eps, monkeypatch):
    class P(BaseProvider):
        name = "plug"
        env_var = "PLUG_KEY"

        def __init__(self, api_key):
            self.key = api_key

        async def complete(self, request):
            raise NotImplementedError

        async def stream(self, request):
            raise NotImplementedError

        def count_tokens(self, text):
            return 0

    fake_eps[plugins.PROVIDER_GROUP] = [EP("plug", P)]
    monkeypatch.setenv("CAIPS_PLUGINS", "plug")
    monkeypatch.setenv("PLUG_KEY", "k")
    try:
        res = providers.load_providers(["plug"])
        assert res.providers["plug"].key == "k"
    finally:
        providers.PROVIDER_MAP.pop("plug", None)


def test_build_runtime_config_allow_list(fake_eps):
    fake_eps[plugins.TOOL_GROUP] = [EP("p", lambda: [tool("plugtool")])]
    assert build_runtime({"plugins": ["p"]}).registry.get("plugtool") is not None
    assert build_runtime({}).registry.get("plugtool") is None
