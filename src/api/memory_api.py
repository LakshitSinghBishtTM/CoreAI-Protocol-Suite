"""api/memory_api.py: per-user long-term memory (write, view, search, forget).

The owner is always the authenticated subject, never a request field, so one
caller can't read or erase another's memory. Stored at ``COREAI_MEMORY_DB``
(default ``data/memory.db``); ``COREAI_MEMORY_RETENTION_DAYS`` limits age.
"""

import os

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.auth import AuthContext, require_scope

router = APIRouter(tags=["memory"])
read = require_scope("memory:read")
write = require_scope("memory:write")


def get_store():
    from api.server import app_state
    from memory import MemoryStore

    store = app_state.get("memory")
    if store is None:
        path = os.getenv("COREAI_MEMORY_DB", "data/memory.db")
        if path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        store = MemoryStore(
            path,
            retention_days=float(os.getenv("COREAI_MEMORY_RETENTION_DAYS", "0")),
        )
        app_state["memory"] = store
    return store


class MemoryBody(BaseModel):
    value: str


@router.put("/v1/memory/{key}")
async def put_memory(
    key: str, body: MemoryBody, ctx: AuthContext = Depends(write), ns: str = "default"
):
    try:
        get_store().remember(ctx.subject, key, body.value, ns)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"ns": ns, "key": key}


@router.get("/v1/memory")
async def list_memory(ctx: AuthContext = Depends(read), ns: str | None = None):
    return {"data": get_store().view(ctx.subject, ns)}


@router.get("/v1/memory/search")
async def search_memory(
    q: str, ctx: AuthContext = Depends(read), ns: str | None = None, limit: int = 5
):
    return {"data": get_store().search(ctx.subject, q, ns, max(1, min(limit, 50)))}


@router.get("/v1/memory/{key}")
async def get_memory(key: str, ctx: AuthContext = Depends(read), ns: str = "default"):
    value = get_store().recall(ctx.subject, key, ns)
    if value is None:
        raise HTTPException(404, "Memory not found.")
    return {"ns": ns, "key": key, "value": value}


@router.delete("/v1/memory/{key}")
async def forget_key(key: str, ctx: AuthContext = Depends(write), ns: str = "default"):
    return {"deleted": get_store().forget(ctx.subject, key, ns)}


@router.delete("/v1/memory")
async def forget_all(ctx: AuthContext = Depends(write), ns: str | None = None):
    """Forget a whole namespace, or everything this caller has stored."""
    return {"deleted": get_store().forget(ctx.subject, None, ns)}
