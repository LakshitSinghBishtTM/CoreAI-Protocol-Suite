"""api/keys_api.py: manage provider API keys at runtime (admin only).

Keys are stored encrypted (coreai/keystore.py), applied to the running router
immediately, and never returned: listings show only a masked tail.
"""

import asyncio
import os

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.auth import AuthContext, require_scope
from coreai import keystore
from providers import PROVIDER_MAP, fingerprint, load_providers
from providers.catalog import CATALOG, EXTRA_ENV

router = APIRouter(tags=["keys"])
admin = require_scope("admin")


class KeyBody(BaseModel):
    provider: str
    api_key: str = ""
    label: str = ""
    base_url: str = ""
    model: str = ""


def _live() -> dict:
    from api.server import get_router

    return get_router().providers


async def reload_providers() -> dict[str, str]:
    """Rebuild env- and store-backed providers and apply the difference to the
    running router. Returns {name: problem} for entries that could not load."""
    from api.server import app_state, get_router

    rt = get_router()
    result = load_providers(include_local_yml=False)
    fresh, live = result.providers, rt.providers
    problems = dict(result.failed)
    for name, p in list(live.items()):
        if getattr(p, "managed", False) and name not in fresh:
            rt.drop_provider(name)
    for name, p in fresh.items():
        cur = live.get(name)
        if cur is not None and not getattr(cur, "managed", False):
            problems[name] = "name clashes with a provider from config/local.yml"
            continue
        if cur is None or fingerprint(cur) != fingerprint(p):
            if getattr(p, "is_local", False):
                await p.refresh_models()  # type: ignore[attr-defined]
            rt.set_provider(name, p)
    old = app_state.get("local_monitor")
    if old is not None:
        old.cancel()
    from providers.local import monitor_local

    app_state["local_monitor"] = asyncio.create_task(monitor_local(live))
    return problems


@router.get("/v1/keys/catalog")
async def catalog(ctx: AuthContext = Depends(admin)):
    return {
        "data": [
            {
                "id": lab.id,
                "label": lab.label,
                "region": lab.region,
                "kind": lab.kind,
                "env_var": lab.env_var,
                "base_url": lab.base_url,
                "model": lab.model,
            }
            for lab in CATALOG
        ]
    }


@router.get("/v1/keys")
async def list_keys(ctx: AuthContext = Depends(admin)):
    try:
        stored = keystore.list_entries()
    except keystore.KeyStoreError as e:
        raise HTTPException(503, str(e))
    live = _live()
    rows = []
    env = {n: v for n, (_, v) in PROVIDER_MAP.items()} | EXTRA_ENV
    for prov, var in env.items():
        if os.getenv(var, "").strip():
            rows.append(
                {
                    "id": f"env:{prov}",
                    "provider": prov,
                    "label": "env",
                    "key": "…",
                    "source": "environment",
                    "loaded": prov in live,
                }
            )
    for entry in stored:
        name = (
            entry["label"]
            if entry["provider"] in ("local", "custom")
            else entry["provider"]
        )
        rows.append(
            {**entry, "source": "store", "loaded": name in live and entry["readable"]}
        )
    return {"data": rows}


@router.post("/v1/keys", status_code=201)
async def add_key(body: KeyBody, ctx: AuthContext = Depends(admin)):
    try:
        entry = keystore.add(
            body.provider, body.api_key, body.label, body.base_url, body.model
        )
    except keystore.KeyStoreError as e:
        raise HTTPException(400, str(e))
    problems = await reload_providers()
    name = entry["label"] if body.provider in ("local", "custom") else body.provider
    return {
        **entry,
        "loaded": name in _live() and name not in problems,
        "problem": problems.get(name)
        or problems.get(f"{body.provider}/{entry['label']}"),
    }


@router.delete("/v1/keys/{entry_id}")
async def delete_key(entry_id: str, ctx: AuthContext = Depends(admin)):
    if entry_id.startswith("env:"):
        raise HTTPException(
            400, "environment keys are set outside the gateway; unset the variable"
        )
    if not keystore.remove(entry_id):
        raise HTTPException(404, "no such key")
    await reload_providers()
    return {"deleted": entry_id}
