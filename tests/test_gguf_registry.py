import pytest

from localrt.gguf import GGUFError, describe, read_metadata
from localrt.registry import ModelRegistry
from tests.gguf_helpers import write_gguf

KV = {
    "general.architecture": "llama",
    "general.name": "Tiny",
    "general.file_type": 15,
    "general.license": "mit",
    "llama.context_length": 4096,
    "tokenizer.chat_template": "{% if tools %}x{% endif %}",
    "tokenizer.ggml.tokens": [f"t{i}" for i in range(500)],
}


def test_describe(tmp_path):
    d = describe(write_gguf(tmp_path / "tiny.gguf", KV))
    assert d["architecture"] == "llama" and d["quantization"] == "Q4_K_M"
    assert d["context_length"] == 4096 and d["license"] == "mit"


def test_rejects_non_gguf(tmp_path):
    p = tmp_path / "x.gguf"
    p.write_bytes(b"nope" * 10)
    with pytest.raises(GGUFError):
        read_metadata(p)


def test_truncated(tmp_path):
    p = write_gguf(tmp_path / "t.gguf", KV)
    p.write_bytes(p.read_bytes()[:40])
    with pytest.raises(GGUFError):
        read_metadata(p)


def test_registry_scan_caps_delete(tmp_path):
    write_gguf(tmp_path / "tiny.gguf", KV)
    (tmp_path / "bad.gguf").write_bytes(b"junk")
    reg = ModelRegistry(tmp_path, {"tiny": {"vision": True}})
    assert list(reg.scan()) == ["tiny"]
    caps = reg.get("tiny")["capabilities"]
    assert caps["tools"] and caps["vision"]
    assert "chat_template" not in reg.list()[0]
    assert reg.delete("tiny") and not (tmp_path / "tiny.gguf").exists()
