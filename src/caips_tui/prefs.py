"""Saved TUI preferences (chat defaults and theme), kept next to the token store.

Lives in ``$CAIPS_CONFIG_DIR/tui.json`` (default ``~/.config/caips``). It never holds
secrets; the token stays in ``credentials.json``. A missing or damaged file means defaults.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "model": "",
    "provider": "",
    "policy": "",
    "temperature": "0.7",
    "max_tokens": "1024",
    "turns": "",
    "system": "",
    "reasoning": "off",  # off | brief | deep
    "tools": False,
    "stream": True,
    "theme": "caips-dark",  # caips-dark | caips-light
}


def _path() -> Path:
    base = os.getenv("CAIPS_CONFIG_DIR")
    if not base:
        xdg = os.getenv("XDG_CONFIG_HOME") or str(Path.home() / ".config")
        base = str(Path(xdg) / "caips")
    return Path(base) / "tui.json"


def load() -> dict[str, Any]:
    """Defaults overlaid with whatever valid values the file holds."""
    out = dict(DEFAULTS)
    try:
        raw = json.loads(_path().read_text())
    except (OSError, ValueError):
        return out
    if isinstance(raw, dict):
        for k, default in DEFAULTS.items():
            if k in raw and isinstance(raw[k], type(default)):
                out[k] = raw[k]
    return out


def save(values: dict[str, Any]) -> Path:
    """Write the known keys atomically; unknown keys are dropped."""
    data = {k: values.get(k, d) for k, d in DEFAULTS.items()}
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, p)
    return p
