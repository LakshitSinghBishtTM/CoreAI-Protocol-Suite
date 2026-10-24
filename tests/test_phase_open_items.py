"""Guardrail filters, Python sandbox, Vulkan detection and the local-models CLI."""

import json
import shutil

import httpx
import pytest

from guardrails.filters import FilterPolicy, apply
from localrt import cli, hardware
from tools.builtin import HttpFetcher
from tools.registry import ToolError
from tools.sandbox import PythonSandbox


def test_secret_blocks_pii_redacts_and_reasons_hide_values():
    p = FilterPolicy()
    r = apply("key sk-abcdefghijklmnopqrstuvwx", p)
    assert not r.allowed and r.reasons == ["secret:api_key"]
    r = apply("mail bob@example.com", p)
    assert (
        r.allowed
        and "bob@example.com" not in r.text
        and r.reasons == ["redacted:email"]
    )
    assert apply("hello", p).text == "hello"


def test_blocked_topics_regex_and_limits():
    p = FilterPolicy.from_dict(
        {"blocked_topics": ["lock picking", r"re:\bbomb\s+making\b"], "max_chars": 50}
    )
    assert apply("teach me Lock Picking", p).reasons == ["topic:lock picking"]
    assert not apply("Bomb  making tips", p).allowed
    assert apply("x" * 51, p).reasons == ["too_long:50"]
    with pytest.raises(ValueError):
        FilterPolicy.from_dict({"blocked_topics": ["re:("]})
    with pytest.raises(ValueError):
        FilterPolicy.from_dict({"blocked_topics": "nope"})


needs_unshare = pytest.mark.skipif(shutil.which("unshare") is None, reason="no unshare")


async def test_sandbox_runs_and_times_out():
    sb = PythonSandbox(timeout_s=3, allow_network=True)
    assert "exit_code=0\n4" in await sb.run("print(2+2)")
    with pytest.raises(ToolError, match="exceeded"):
        await PythonSandbox(timeout_s=1, allow_network=True).run("while True: pass")


async def test_sandbox_env_is_empty_and_memory_capped():
    sb = PythonSandbox(timeout_s=5, memory_mb=128, allow_network=True)
    out = await sb.run("import os; print(sorted(os.environ))")
    assert "HOME" not in out and "OPENAI_API_KEY" not in out
    out = await sb.run("x = bytearray(1024**3)")
    assert "exit_code=0" not in out and "MemoryError" in out


async def test_sandbox_caps_output_while_reading(monkeypatch):
    monkeypatch.setattr("tools.sandbox.MAX_OUTPUT", 1024)
    out = await PythonSandbox(timeout_s=5, allow_network=True).run(
        "import sys; sys.stdout.write('x' * 10_000_000)"
    )
    assert out.endswith("\n[output truncated]")
    assert out.split("\n", 1)[1].count("x") == 1024


async def test_sandbox_timeout_still_applies_after_child_closes_output():
    code = "import os; os.close(1); os.close(2); exec('while True: pass')"
    with pytest.raises(ToolError, match="exceeded"):
        await PythonSandbox(timeout_s=0.2, allow_network=True).run(code)


async def test_http_fetcher_stops_reading_at_the_response_limit():
    class Chunks(httpx.AsyncByteStream):
        def __init__(self):
            self.reads = 0
            self.closed = False

        async def __aiter__(self):
            for chunk in (b"a" * 8192, b"b" * 8192, b"c" * 8192):
                self.reads += 1
                yield chunk

        async def aclose(self):
            self.closed = True

    stream = Chunks()
    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))
    fetcher = HttpFetcher(
        ["example.com"],
        transport=transport,
        resolver=lambda host: ["93.184.216.34"],
        max_bytes=10,
    )
    assert await fetcher.fetch("https://example.com/large") == "a" * 10
    assert stream.reads == 1 and stream.closed


async def test_sandbox_refuses_without_network_isolation():
    sb = PythonSandbox()
    sb._unshare = None
    with pytest.raises(ToolError, match="isolation"):
        await sb.run("print(1)")


@needs_unshare
async def test_sandbox_has_no_network_when_isolation_works():
    sb = PythonSandbox(timeout_s=5)
    probe = (
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1',80),2);print('NET')\n"
        "except OSError:print('NONET')"
    )
    try:
        out = await sb.run(probe)
    except ToolError:
        pytest.skip("unshare not permitted here")
    if "exit_code=0" in out:
        assert "NONET" in out


def test_vulkan_detected_only_when_nothing_better(monkeypatch):
    monkeypatch.setattr(hardware, "_nvidia_vram", lambda: 0)
    monkeypatch.setattr(hardware, "_vulkan_present", lambda: True)
    monkeypatch.setattr(hardware.platform, "system", lambda: "Linux")
    hw = hardware.detect()
    assert hw.backend == "vulkan" and hw.vram_bytes == hw.ram_bytes // 2
    monkeypatch.setattr(hardware, "_nvidia_vram", lambda: 8 * 1024**3)
    assert hardware.detect().backend == "cuda"
    monkeypatch.setattr(hardware, "_nvidia_vram", lambda: 0)
    monkeypatch.setattr(hardware, "_vulkan_present", lambda: False)
    assert hardware.detect().backend == "cpu"


def test_cli_list_inspect_delete_and_missing(tmp_path, capsys):
    assert cli.main(["--dir", str(tmp_path), "list"]) == 0
    assert capsys.readouterr().out == ""
    assert cli.main(["--dir", str(tmp_path), "inspect", "nope"]) == 1
    assert cli.main(["--dir", str(tmp_path), "delete", "nope"]) == 1
    assert cli.main(["--dir", str(tmp_path), "hardware"]) == 0
    assert "backend" in json.loads(capsys.readouterr().out.splitlines()[-1])
