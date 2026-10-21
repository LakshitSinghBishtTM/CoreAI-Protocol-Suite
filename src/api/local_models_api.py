"""api/local_models_api.py: list, inspect, pull and delete local GGUF models (admin only)."""

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.auth import AuthContext, require_scope

router = APIRouter(tags=["local-models"])
admin = require_scope("admin")


def _managed():
    from api.server import app_state
    from localrt.provider import ManagedLocalProvider

    for p in (app_state.get("providers") or {}).values():
        if isinstance(p, ManagedLocalProvider):
            return p
    raise HTTPException(
        404, "Managed local runtime is not enabled (see config/local.yml)."
    )


class PullBody(BaseModel):
    model: str  # org/repo:QUANT


@router.get("/v1/local/models")
async def list_models(ctx: AuthContext = Depends(admin)):
    p = _managed()
    await p.refresh_models()
    loaded = set(p.supervisor.loaded)
    return {"data": [{**m, "loaded": m["id"] in loaded} for m in p.registry.list()]}


@router.get("/v1/local/models/{model_id}")
async def inspect_model(model_id: str, ctx: AuthContext = Depends(admin)):
    p = _managed()
    info = p.registry.get(model_id)
    if info is None:
        raise HTTPException(404, "Model not found.")
    public = next(m for m in p.registry.list() if m["id"] == model_id)
    return {**public, "loaded": model_id in p.supervisor.loaded}


@router.post("/v1/local/models/pull", status_code=202)
async def pull_model(body: PullBody, ctx: AuthContext = Depends(admin)):
    from localrt.download import DownloadError, pull

    p = _managed()
    try:
        path = await asyncio.to_thread(pull, body.model, p.registry.directory)
    except DownloadError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # network errors
        raise HTTPException(502, f"Download failed: {e}")
    await p.refresh_models()
    return {"id": path.stem, "file": path.name}


@router.delete("/v1/local/models/{model_id}")
async def delete_model(model_id: str, ctx: AuthContext = Depends(admin)):
    p = _managed()
    p.supervisor.unload(model_id)
    if not p.registry.delete(model_id):
        raise HTTPException(404, "Model not found.")
    p.models.discard(model_id)
    return {"deleted": model_id}


@router.post("/v1/local/models/{model_id}/unload")
async def unload_model(model_id: str, ctx: AuthContext = Depends(admin)):
    return {"unloaded": _managed().supervisor.unload(model_id)}
