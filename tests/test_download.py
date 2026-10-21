import hashlib

import httpx
import pytest

from localrt.download import DownloadError, download, pull, resolve

DATA = b"x" * 5000
SHA = hashlib.sha256(DATA).hexdigest()


def handler(request: httpx.Request) -> httpx.Response:
    p = request.url.path
    if p.endswith("/tree/main"):
        return httpx.Response(200, json=[
            {"type": "file", "path": "m-Q4_K_M.gguf", "size": len(DATA), "lfs": {"oid": SHA}},
            {"type": "file", "path": "m-Q8_0.gguf", "size": 1, "lfs": {"oid": "0"}},
            {"type": "file", "path": "README.md", "size": 1},
        ])  # fmt: skip
    rng = request.headers.get("range")
    if rng:
        start = int(rng.split("=")[1].rstrip("-"))
        if start >= len(DATA):
            return httpx.Response(416)
        return httpx.Response(206, content=DATA[start:])
    return httpx.Response(200, content=DATA)


@pytest.fixture
def client():
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_resolve():
    files = ["a-Q4_K_M.gguf", "a-Q8_0.gguf", "x.md"]
    assert resolve("o/r:q8_0", files) == "a-Q8_0.gguf"
    with pytest.raises(DownloadError):
        resolve("o/r", files)
    with pytest.raises(DownloadError):
        resolve("o/r:Q2_K", files)


def test_pull_verifies(tmp_path, client):
    p = pull("o/r:Q4_K_M", tmp_path, client)
    assert p.read_bytes() == DATA and not list(tmp_path.glob("*.part"))


def test_resume(tmp_path, client):
    (tmp_path / "m.gguf.part").write_bytes(DATA[:2000])
    p = download("https://h/f", tmp_path / "m.gguf", sha256=SHA, client=client)
    assert p.read_bytes() == DATA


def test_bad_hash_deleted(tmp_path, client):
    with pytest.raises(DownloadError, match="SHA-256"):
        download("https://h/f", tmp_path / "m.gguf", sha256="0" * 64, client=client)
    assert not list(tmp_path.iterdir())
