"""Per-gateway API token store shared by `caips` and `caips-tui`.

Tokens live in ``$CAIPS_CONFIG_DIR`` (default ``$XDG_CONFIG_HOME/caips`` or
``~/.config/caips``) in ``credentials.json``, mode 0600, keyed by gateway URL.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional


def _path() -> Path:
    base = os.getenv("CAIPS_CONFIG_DIR")
    if not base:
        xdg = os.getenv("XDG_CONFIG_HOME") or str(Path.home() / ".config")
        base = str(Path(xdg) / "caips")
    return Path(base) / "credentials.json"


def _read() -> dict:
    try:
        data = json.loads(_path().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load(url: str) -> Optional[str]:
    v = _read().get(url.rstrip("/"))
    return v if isinstance(v, str) and v else None


def save(url: str, key: str) -> Path:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = _read()
    data[url.rstrip("/")] = key
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, p)
    return p


def delete(url: str) -> bool:
    data = _read()
    if data.pop(url.rstrip("/"), None) is None:
        return False
    p = _path()
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    return True


def resolve(url: str) -> Optional[str]:
    """``CAIPS_API_KEY`` wins (CI, Docker); otherwise the stored token."""
    return os.getenv("CAIPS_API_KEY") or load(url)
