"""Provider API keys managed at runtime (through the TUI, CLI or ``/v1/keys``).

Keys are encrypted at rest with Fernet, using a key derived from
``CAIPS_KEYSTORE_SECRET`` (falling back to ``SECRET_KEY``). Changing that secret
makes stored keys unreadable; they are skipped with a warning, never guessed.
The file is ``$CAIPS_KEYSTORE`` (default ``data/provider_keys.json``), mode 0600.
Secrets are never returned by any API: only a masked tail.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from loguru import logger

from providers.catalog import BY_ID

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_lock = threading.Lock()


class KeyStoreError(ValueError):
    pass


def _path() -> Path:
    return Path(os.getenv("CAIPS_KEYSTORE") or "data/provider_keys.json")


def _fernet() -> Fernet:
    secret = os.getenv("CAIPS_KEYSTORE_SECRET") or os.getenv("SECRET_KEY") or ""
    if not secret:
        raise KeyStoreError("set SECRET_KEY (or CAIPS_KEYSTORE_SECRET) to store keys")
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))


def _read() -> list[dict]:
    try:
        data = json.loads(_path().read_text())
    except (OSError, ValueError):
        return []
    return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []


def _write(entries: list[dict]) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(entries, f)
    os.replace(tmp, p)


def mask(key: str) -> str:
    return f"…{key[-4:]}" if len(key) > 8 else "…"


def public(e: dict, secret: Optional[str] = None) -> dict:
    out = {k: e.get(k, "") for k in ("id", "provider", "label", "base_url", "model", "created")}
    out["key"] = mask(secret) if secret else ("" if not e.get("token") else "…")
    return out


def list_entries() -> list[dict]:
    """Entries without secrets (masked tail only)."""
    raw = _read()
    if not raw:
        return []
    f = _fernet()
    out = []
    for e in raw:
        readable = True
        try:
            secret = f.decrypt(e["token"].encode()).decode() if e.get("token") else ""
        except (InvalidToken, KeyError):
            secret, readable = "", False
        item = public(e, secret)
        item["readable"] = readable
        out.append(item)
    return out


def load_secrets() -> list[dict]:
    """Entries with decrypted ``api_key``, for building providers. Unreadable ones are skipped."""
    raw = _read()
    if not raw:
        return []
    f = _fernet()
    out = []
    for e in raw:
        try:
            key = f.decrypt(e["token"].encode()).decode() if e.get("token") else ""
        except (InvalidToken, KeyError):
            logger.warning(
                f"stored key {e.get('id')} ({e.get('provider')}) cannot be decrypted "
                "(was SECRET_KEY changed?); skipped"
            )
            continue
        out.append({**{k: v for k, v in e.items() if k != "token"}, "api_key": key})
    return out


def add(
    provider: str,
    api_key: str = "",
    label: str = "",
    base_url: str = "",
    model: str = "",
) -> dict:
    lab = BY_ID.get(provider)
    if lab is None:
        raise KeyStoreError(f"unknown provider '{provider}'")
    api_key, label = api_key.strip(), label.strip().lower()
    base_url, model = base_url.strip().rstrip("/"), model.strip()
    if lab.kind in ("native", "compat", "custom") and not api_key:
        raise KeyStoreError("api_key is required")
    if lab.kind in ("local", "custom"):
        # each one is its own provider, named by its label
        if not NAME_RE.fullmatch(label):
            raise KeyStoreError("label (the provider name) must be a-z, 0-9, '-' or '_'")
        if label in BY_ID:
            raise KeyStoreError(f"label '{label}' is reserved")
        from providers.local import PRESETS

        base_url = base_url or PRESETS.get(label, "")
        if not base_url:
            raise KeyStoreError("base_url is required (or label one of: " + ", ".join(PRESETS) + ")")
    elif base_url and lab.kind == "native":
        raise KeyStoreError(f"{provider} uses its SDK's endpoint; base_url is not supported")
    if base_url and not base_url.startswith(("http://", "https://")):
        raise KeyStoreError("base_url must start with http:// or https://")
    with _lock:
        entries = _read()
        same = [e for e in entries if e.get("provider") == provider]
        if lab.kind in ("local", "custom"):
            if any(e.get("label") == label for e in entries if e.get("provider") in ("local", "custom")):
                raise KeyStoreError(f"a server named '{label}' already exists")
        else:
            label = label or str(len(same) + 1)
            if not NAME_RE.fullmatch(label):
                raise KeyStoreError("label must be a-z, 0-9, '-' or '_'")
            if any(e.get("label") == label for e in same):
                raise KeyStoreError(f"{provider} already has a key labelled '{label}'")
        entry = {
            "id": uuid.uuid4().hex[:8],
            "provider": provider,
            "label": label,
            "base_url": base_url,
            "model": model,
            "created": int(time.time()),
            "token": _fernet().encrypt(api_key.encode()).decode() if api_key else "",
        }
        entries.append(entry)
        _write(entries)
    return public(entry, api_key)


def remove(entry_id: str) -> bool:
    with _lock:
        entries = _read()
        kept = [e for e in entries if e.get("id") != entry_id]
        if len(kept) == len(entries):
            return False
        _write(kept)
    return True
