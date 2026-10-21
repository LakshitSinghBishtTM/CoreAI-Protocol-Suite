"""Minimal GGUF header reader: metadata only, never loads tensors."""

import struct
from pathlib import Path
from typing import Any, BinaryIO

MAGIC = b"GGUF"
MAX_STR = 16 * 1024 * 1024  # a chat template is small; refuse absurd lengths
MAX_ARRAY_KEEP = 64  # keep only a prefix of big arrays (e.g. the vocab)

_SCALARS = {
    0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i",
    6: "<f", 7: "<?", 10: "<Q", 11: "<q", 12: "<d",
}  # fmt: skip

FILE_TYPES = {
    0: "F32", 1: "F16", 2: "Q4_0", 3: "Q4_1", 7: "Q8_0", 8: "Q5_0", 9: "Q5_1",
    10: "Q2_K", 11: "Q3_K_S", 12: "Q3_K_M", 13: "Q3_K_L", 14: "Q4_K_S",
    15: "Q4_K_M", 16: "Q5_K_S", 17: "Q5_K_M", 18: "Q6_K", 32: "BF16",
}  # fmt: skip


class GGUFError(ValueError):
    pass


def _read(f: BinaryIO, n: int) -> bytes:
    b = f.read(n)
    if len(b) != n:
        raise GGUFError("truncated GGUF file")
    return b


def _str(f: BinaryIO) -> str:
    (n,) = struct.unpack("<Q", _read(f, 8))
    if n > MAX_STR:
        raise GGUFError("string too long")
    return _read(f, n).decode("utf-8", "replace")


def _value(f: BinaryIO, t: int) -> Any:
    if t in _SCALARS:
        fmt = _SCALARS[t]
        return struct.unpack(fmt, _read(f, struct.calcsize(fmt)))[0]
    if t == 8:
        return _str(f)
    if t == 9:
        (et,) = struct.unpack("<I", _read(f, 4))
        (n,) = struct.unpack("<Q", _read(f, 8))
        out = []
        for i in range(n):
            v = _value(f, et)
            if i < MAX_ARRAY_KEEP:
                out.append(v)
        return out
    raise GGUFError(f"unknown value type {t}")


def read_metadata(path: str | Path) -> dict[str, Any]:
    """Return the key/value metadata of a GGUF file."""
    with open(path, "rb") as f:
        if _read(f, 4) != MAGIC:
            raise GGUFError("not a GGUF file")
        (version,) = struct.unpack("<I", _read(f, 4))
        if version not in (2, 3):
            raise GGUFError(f"unsupported GGUF version {version}")
        _tensors, n_kv = struct.unpack("<QQ", _read(f, 16))
        if n_kv > 100_000:
            raise GGUFError("implausible metadata count")
        meta: dict[str, Any] = {"_version": version}
        for _ in range(n_kv):
            key = _str(f)
            (t,) = struct.unpack("<I", _read(f, 4))
            meta[key] = _value(f, t)
        return meta


def describe(path: str | Path) -> dict[str, Any]:
    """Registry-ready facts: architecture, size, quantization, context, template."""
    p = Path(path)
    m = read_metadata(p)
    arch = m.get("general.architecture", "unknown")
    ft = m.get("general.file_type")
    return {
        "name": m.get("general.name") or p.stem,
        "file": p.name,
        "size_bytes": p.stat().st_size,
        "architecture": arch,
        "parameters": m.get("general.size_label"),
        "quantization": FILE_TYPES.get(ft, f"type{ft}") if ft is not None else None,
        "context_length": m.get(f"{arch}.context_length"),
        "n_layers": m.get(f"{arch}.block_count"),
        "chat_template": m.get("tokenizer.chat_template"),
        "license": m.get("general.license"),
        "source": m.get("general.source.url") or m.get("general.url"),
    }
