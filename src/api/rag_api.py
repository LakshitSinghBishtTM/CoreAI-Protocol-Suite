"""api/rag_api.py: ingest, list, delete and search document collections.

Collections are scoped to the authenticated subject. Embeddings go through the
gateway (cloud or local). Store: ``COREAI_RAG_DB`` (default ``data/rag.db``).
"""

import os

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.auth import AuthContext, require_scope

router = APIRouter(tags=["rag"])
read = require_scope("rag:read")
write = require_scope("rag:write")

MAX_DOC_CHARS = 2_000_000


def get_store():
    from api.server import app_state, get_router
    from rag.store import VectorStore

    store = app_state.get("rag")
    if store is None:

        async def _embed(texts):
            return (await get_router().embed(texts))[0]

        path = os.getenv("COREAI_RAG_DB", "data/rag.db")
        if path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        store = VectorStore(path, _embed)
        app_state["rag"] = store
    return store


class DocumentBody(BaseModel):
    source: str = Field(min_length=1, max_length=500)
    text: str = Field(min_length=1)
    meta: dict | None = None


class SearchBody(BaseModel):
    query: str = Field(min_length=1)
    k: int = Field(4, ge=1, le=20)


@router.post("/v1/rag/{collection}/documents", status_code=201)
async def ingest(
    collection: str, body: DocumentBody, ctx: AuthContext = Depends(write)
):
    if len(body.text) > MAX_DOC_CHARS:
        raise HTTPException(413, f"Document exceeds {MAX_DOC_CHARS} characters.")
    try:
        n = await get_store().ingest_text(
            ctx.subject, collection, body.source, body.text, body.meta
        )
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"collection": collection, "source": body.source, "chunks": n}


@router.get("/v1/rag/{collection}/documents")
async def list_documents(collection: str, ctx: AuthContext = Depends(read)):
    return {"data": get_store().sources(ctx.subject, collection)}


@router.delete("/v1/rag/{collection}/documents/{source:path}")
async def delete_document(
    collection: str, source: str, ctx: AuthContext = Depends(write)
):
    n = get_store().delete_source(ctx.subject, collection, source)
    if not n:
        raise HTTPException(404, "Document not found.")
    return {"deleted": source, "chunks": n}


@router.post("/v1/rag/{collection}/search")
async def search(collection: str, body: SearchBody, ctx: AuthContext = Depends(read)):
    try:
        hits = await get_store().search(ctx.subject, collection, body.query, body.k)
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    return {"data": hits}
