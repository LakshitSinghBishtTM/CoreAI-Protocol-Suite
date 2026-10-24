import hashlib
import io
import zipfile

import httpx
import pytest

from localrt import runtime


def _zip(files):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for n, d in files.items():
            z.writestr(n, d)
    return b.getvalue()


def _client(blob, digest=None, names=None):
    names = names or [
        "llama-b1-bin-ubuntu-x64.zip",
        "llama-b1-bin-ubuntu-vulkan-x64.zip",
        "llama-b1-bin-macos-arm64.zip",
    ]
    d = digest if digest is not None else "sha256:" + hashlib.sha256(blob).hexdigest()

    def h(req):
        if "releases" in req.url.path:
            return httpx.Response(
                200,
                json={
                    "tag_name": "b1",
                    "assets": [
                        {
                            "name": n,
                            "digest": d,
                            "browser_download_url": f"https://dl/{n}",
                        }
                        for n in names
                    ],
                },
            )
        return httpx.Response(200, content=blob)

    return httpx.Client(transport=httpx.MockTransport(h))


def test_platform_pick_prefers_plain_cpu_build():
    want, avoid = runtime.platform_keywords("Linux", "x86_64")
    a = runtime.pick_asset(
        [
            {"name": "llama-b1-bin-ubuntu-vulkan-x64.zip"},
            {"name": "llama-b1-bin-ubuntu-x64.zip"},
        ],
        want,
        avoid,
    )
    assert a["name"] == "llama-b1-bin-ubuntu-x64.zip"
    with pytest.raises(runtime.RuntimeError_):
        runtime.platform_keywords("Plan9", "x86_64")


def test_install_verifies_and_unpacks(tmp_path):
    blob = _zip({"build/bin/llama-server": "#!/bin/sh\n"})
    p = runtime.install(
        "b1", dest=tmp_path, client=_client(blob), system="Linux", machine="x86_64"
    )
    assert p.endswith("llama-server") and (tmp_path / "b1").is_dir()


def test_install_refuses_bad_hash_missing_digest_and_traversal(tmp_path):
    blob = _zip({"bin/llama-server": "x"})
    kw = dict(dest=tmp_path, system="Linux", machine="x86_64")
    with pytest.raises(runtime.RuntimeError_, match="mismatch"):
        runtime.install("b1", client=_client(blob, "sha256:" + "0" * 64), **kw)
    with pytest.raises(runtime.RuntimeError_, match="digest"):
        runtime.install("b1", client=_client(blob, ""), **kw)
    evil = _zip({"../evil": "x"})
    with pytest.raises(runtime.RuntimeError_, match="unsafe"):
        runtime.install("b1", client=_client(evil), **kw)
    assert not (tmp_path.parent / "evil").exists()


def test_find_order(tmp_path, monkeypatch):
    exe = tmp_path / "llama-server"
    exe.write_text("x")
    monkeypatch.setenv("LLAMA_SERVER", str(exe))
    assert runtime.find() == str(exe)
    assert runtime.find("/nonexistent") == str(exe)
