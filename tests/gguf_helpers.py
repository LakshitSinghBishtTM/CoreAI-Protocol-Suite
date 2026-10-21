import struct
from pathlib import Path


def _s(x: str) -> bytes:
    b = x.encode()
    return struct.pack("<Q", len(b)) + b


def write_gguf(path: Path, kv: dict) -> Path:
    out = b"GGUF" + struct.pack("<IQQ", 3, 0, len(kv))
    for k, v in kv.items():
        out += _s(k)
        if isinstance(v, str):
            out += struct.pack("<I", 8) + _s(v)
        elif isinstance(v, list):
            out += struct.pack("<IIQ", 9, 8, len(v)) + b"".join(_s(i) for i in v)
        else:
            out += struct.pack("<II", 4, v)
    path.write_bytes(out)
    return path
