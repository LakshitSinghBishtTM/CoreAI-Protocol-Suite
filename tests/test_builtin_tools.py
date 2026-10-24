"""Phase 6: built-in tools and their safety limits."""

import httpx
import pytest

from tools import ToolError, ToolRegistry
from tools.builtin import HttpFetcher, Workspace, register_builtins, safe_eval


@pytest.mark.parametrize(
    "expr,out",
    [
        ("1+2*3", 7),
        ("(1+2)*3", 9),
        ("2**10", 1024),
        ("-4+10/4", -1.5),
        ("sqrt(16)+pi*0", 4.0),
    ],
)
def test_calculator_math(expr, out):
    assert safe_eval(expr) == out


@pytest.mark.parametrize(
    "expr",
    [
        "__import__('os').system('x')",
        "open('f')",
        "a+1",
        "(1).real",
        "[1]",
        "2**100000",
        "1/0",
        "1+",
        "x" * 600,
    ],
)
def test_calculator_rejects_everything_else(expr):
    with pytest.raises(ToolError):
        safe_eval(expr)


def test_workspace_blocks_escape(tmp_path):
    ws = Workspace(tmp_path / "w")
    (tmp_path / "w").mkdir()
    ws.write("a/b.txt", "hi")
    assert ws.read("a/b.txt") == "hi"
    for bad in ("../x", "/etc/passwd", "a/../../x"):
        with pytest.raises(ToolError) as e:
            ws.read(bad)
        assert e.value.code == "not_allowed"
    (tmp_path / "secret.txt").write_text("s")
    (tmp_path / "w" / "link").symlink_to(tmp_path / "secret.txt")
    with pytest.raises(ToolError):
        ws.read("link")


def test_workspace_size_limits(tmp_path):
    ws = Workspace(tmp_path, max_bytes=10)
    with pytest.raises(ToolError):
        ws.write("big", "x" * 11)


def fetcher(handler, **kw):
    return HttpFetcher(
        ["example.com"],
        transport=httpx.MockTransport(handler),
        resolver=lambda h: ["93.184.216.34"],
        **kw,
    )


async def test_fetch_allow_list_and_scheme():
    f = fetcher(lambda r: httpx.Response(200, text="ok"))
    assert await f.fetch("https://api.example.com/x") == "ok"
    for bad in ("https://evil.com/", "ftp://example.com/", "https://notexample.com/"):
        with pytest.raises(ToolError):
            await f.fetch(bad)


async def test_fetch_blocks_private_hosts_and_redirects():
    f = HttpFetcher(["example.com"], transport=httpx.MockTransport(lambda r: httpx.Response(200)),
                    resolver=lambda h: ["127.0.0.1"])  # fmt: skip
    with pytest.raises(ToolError):
        await f.fetch("https://example.com/")
    g = fetcher(
        lambda r: httpx.Response(302, headers={"location": "http://169.254.169.254/"})
    )
    with pytest.raises(ToolError):
        await g.fetch("https://example.com/")


async def test_fetch_caps_body():
    f = fetcher(lambda r: httpx.Response(200, text="x" * 500), max_bytes=100)
    assert len(await f.fetch("https://example.com/")) == 100


async def test_pinned_network_backend_uses_only_resolved_addresses():
    from tools.builtin import _PinnedNetworkBackend

    class RecordingBackend:
        def __init__(self):
            self.hosts = []

        async def connect_tcp(self, host, port, **kwargs):
            self.hosts.append((host, port))
            return "stream"

    backend = _PinnedNetworkBackend({"example.com": ["93.184.216.34"]})
    recording = RecordingBackend()
    backend._backend = recording
    assert await backend.connect_tcp("example.com", 443) == "stream"
    assert recording.hosts == [("93.184.216.34", 443)]
    with pytest.raises(OSError, match="not resolved"):
        await backend.connect_tcp("other.example", 443)


async def test_registered_only_when_configured(tmp_path):
    r = ToolRegistry()
    register_builtins(r)
    assert [t.name for t in r.visible(["*"])] == ["calculator"]
    r2 = ToolRegistry()
    register_builtins(r2, Workspace(tmp_path), fetcher(lambda q: httpx.Response(200)))
    names = [t.name for t in r2.visible(["*"])]
    assert names == ["calculator", "file_read", "file_write", "http_fetch"]
    res = await r2.call("calculator", {"expression": "6*7"}, allowed=["calculator"])
    assert res.content == "42"
