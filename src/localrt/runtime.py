"""Find or install a prebuilt ``llama-server`` (llama.cpp release binary).

CaiPS never compiles llama.cpp. ``find`` looks at an explicit path, the
``LLAMA_SERVER`` env var, the managed runtime dir, then ``PATH``. ``install``
downloads an official release asset for this platform from GitHub, verifies
the SHA-256 digest that GitHub publishes for the asset (refuses without one),
and unpacks it safely.

NOTE: no release tag is pinned here because none has been tested yet; pass
``tag`` explicitly (``"latest"`` is accepted but is not a pinned version).
Asset names are matched by platform keywords, since llama.cpp renames them
between releases; unit tests use recorded release JSON, not the live API.
"""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import stat
import tarfile
import zipfile
from pathlib import Path
from typing import Any

import httpx

REPO = "ggml-org/llama.cpp"
API = "https://api.github.com"
EXE = "llama-server.exe" if os.name == "nt" else "llama-server"


class RuntimeError_(Exception):
    pass


def runtime_dir() -> Path:
    base = os.environ.get("CAIPS_RUNTIME_DIR")
    return Path(base) if base else Path.home() / ".cache" / "caips" / "llama.cpp"


def find(explicit: str | None = None) -> str | None:
    """Path of a usable ``llama-server`` or None."""
    for cand in (explicit, os.environ.get("LLAMA_SERVER")):
        if cand and Path(cand).is_file():
            return str(cand)
    d = runtime_dir()
    if d.is_dir():
        hits = sorted(d.rglob(EXE))
        if hits:
            return str(hits[-1])
    return shutil.which("llama-server")


def platform_keywords(
    system: str | None = None, machine: str | None = None, backend: str = "cpu"
) -> tuple[list[str], list[str]]:
    """(must-contain-all, must-contain-none) keywords for the asset name."""
    system = (system or platform.system()).lower()
    m = (machine or platform.machine()).lower()
    arch = "arm64" if m in ("arm64", "aarch64") else "x64"
    os_kw = {"linux": "ubuntu", "darwin": "macos", "windows": "win"}.get(system)
    if os_kw is None:
        raise RuntimeError_(f"no prebuilt llama-server for {system}")
    want = [os_kw, arch]
    accel = ("vulkan", "cuda", "hip", "sycl", "opencl", "rocm", "openvino", "kleidiai")
    if backend != "cpu":
        want.append(backend)
        return want, [k for k in ("cudart",) if backend != "cudart"]
    return want, list(accel) + ["cudart"]


def pick_asset(
    assets: list[dict[str, Any]], want: list[str], avoid: list[str]
) -> dict[str, Any]:
    ok = [
        a
        for a in assets
        if a["name"].endswith((".zip", ".tar.gz"))
        and all(k in a["name"].lower() for k in want)
        and not any(k in a["name"].lower() for k in avoid)
    ]
    if not ok:
        raise RuntimeError_(f"no release asset matches {want} (avoiding {avoid})")
    return sorted(ok, key=lambda a: len(a["name"]))[0]  # plainest name wins


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_extract(archive: Path, dest: Path) -> None:
    root = dest.resolve()

    def check(name: str) -> None:
        if not (root / name).resolve().is_relative_to(root):
            raise RuntimeError_(f"unsafe path in archive: {name}")

    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            for n in z.namelist():
                check(n)
            z.extractall(dest)
    else:
        with tarfile.open(archive) as t:
            for m in t.getmembers():
                check(m.name)
                if m.issym() or m.islnk():
                    check(os.path.join(os.path.dirname(m.name), m.linkname))
                if not (m.isfile() or m.isdir() or m.issym() or m.islnk()):
                    raise RuntimeError_(f"unsupported member type: {m.name}")
            t.extractall(dest)  # members validated above


def install(
    tag: str = "latest",
    backend: str = "cpu",
    dest: Path | None = None,
    client: httpx.Client | None = None,
    system: str | None = None,
    machine: str | None = None,
) -> str:
    """Download, verify and unpack a release; return the ``llama-server`` path."""
    dest = Path(dest) if dest else runtime_dir()
    c = client or httpx.Client(follow_redirects=True, timeout=60)
    ep = "latest" if tag == "latest" else f"tags/{tag}"
    r = c.get(f"{API}/repos/{REPO}/releases/{ep}")
    if r.status_code != 200:
        raise RuntimeError_(f"release lookup failed: HTTP {r.status_code}")
    rel = r.json()
    want, avoid = platform_keywords(system, machine, backend)
    asset = pick_asset(rel.get("assets", []), want, avoid)
    digest = str(asset.get("digest") or "")
    if not digest.startswith("sha256:"):
        raise RuntimeError_(f"{asset['name']}: no published sha256 digest; refusing")
    target = dest / rel["tag_name"]
    target.mkdir(parents=True, exist_ok=True)
    archive = target / asset["name"]
    with c.stream("GET", asset["browser_download_url"]) as resp:
        if resp.status_code != 200:
            raise RuntimeError_(f"download failed: HTTP {resp.status_code}")
        with archive.open("wb") as f:
            for chunk in resp.iter_bytes(1 << 20):
                f.write(chunk)
    if _sha256(archive) != digest.split(":", 1)[1].lower():
        archive.unlink(missing_ok=True)
        raise RuntimeError_(f"{asset['name']}: SHA-256 mismatch; deleted")
    _safe_extract(archive, target)
    archive.unlink(missing_ok=True)
    hits = sorted(target.rglob(EXE))
    if not hits:
        raise RuntimeError_("archive did not contain llama-server")
    hits[0].chmod(hits[0].stat().st_mode | stat.S_IXUSR)
    return str(hits[0])
