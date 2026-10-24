"""Resumable, verified model downloads (Hugging Face by ``org/repo:QUANT``)."""

import hashlib
import shutil
from pathlib import Path

import httpx

HF = "https://huggingface.co"


class DownloadError(RuntimeError):
    pass


def resolve(spec: str, files: list[str]) -> str:
    """Pick the .gguf in ``files`` matching ``spec``'s quantization tag."""
    _, _, quant = spec.partition(":")
    ggufs = [f for f in files if f.lower().endswith(".gguf")]
    if not quant:
        if len(ggufs) == 1:
            return ggufs[0]
        raise DownloadError("Specify a quantization, e.g. org/repo:Q4_K_M")
    hits = [f for f in ggufs if quant.lower() in f.lower()]
    if len(hits) != 1:
        raise DownloadError(f"{len(hits)} files match '{quant}' (need exactly 1)")
    return hits[0]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(
    url: str,
    dest: Path,
    *,
    sha256: str = "",
    expected_size: int = 0,
    client: httpx.Client | None = None,
) -> Path:
    """Download to ``dest`` via a ``.part`` file, resuming with a Range header.

    Checks free disk space first and the SHA-256 (if given) at the end; a bad
    hash deletes the file.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    have = part.stat().st_size if part.exists() else 0
    if expected_size and shutil.disk_usage(dest.parent).free < expected_size - have:
        raise DownloadError("not enough free disk space")
    own = client is None
    c = client or httpx.Client(follow_redirects=True, timeout=30)
    try:
        headers = {"Range": f"bytes={have}-"} if have else {}
        with c.stream("GET", url, headers=headers) as r:
            if r.status_code == 416:  # already complete
                pass
            else:
                r.raise_for_status()
                mode = "ab" if r.status_code == 206 else "wb"
                with open(part, mode) as f:
                    for chunk in r.iter_bytes(1 << 20):
                        f.write(chunk)
    finally:
        if own:
            c.close()
    if expected_size and part.stat().st_size != expected_size:
        raise DownloadError("size mismatch; rerun to resume")
    if sha256 and sha256_file(part) != sha256.lower():
        part.unlink()
        raise DownloadError("SHA-256 mismatch; file deleted")
    part.rename(dest)
    return dest


def hf_files(repo: str, client: httpx.Client | None = None) -> dict[str, dict]:
    """``{filename: {"size":..., "sha256":...}}`` for a Hugging Face repo."""
    own = client is None
    c = client or httpx.Client(timeout=30)
    try:
        r = c.get(f"{HF}/api/models/{repo}/tree/main")
        r.raise_for_status()
        return {
            e["path"]: {
                "size": e.get("size", 0),
                "sha256": (e.get("lfs") or {}).get("oid", ""),
            }
            for e in r.json()
            if e.get("type") == "file"
        }
    finally:
        if own:
            c.close()


def pull(spec: str, models_dir: Path, client: httpx.Client | None = None) -> Path:
    repo = spec.partition(":")[0]
    files = hf_files(repo, client)
    name = resolve(spec, list(files))
    meta = files[name]
    return download(
        f"{HF}/{repo}/resolve/main/{name}",
        models_dir / Path(name).name,
        sha256=meta["sha256"],
        expected_size=meta["size"],
        client=client,
    )
