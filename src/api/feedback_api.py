"""api/feedback_api.py: thumbs up/down and corrections against request ids."""

import os

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.auth import AuthContext, require_scope

router = APIRouter(tags=["feedback"])
read = require_scope("feedback:read")
write = require_scope("feedback:write")


def get_store():
    from api.server import app_state
    from feedback import FeedbackStore

    store = app_state.get("feedback")
    if store is None:
        path = os.getenv("COREAI_FEEDBACK_DB", "data/feedback.db")
        if path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        store = FeedbackStore(path)
        app_state["feedback"] = store
    return store


class FeedbackBody(BaseModel):
    request_id: str
    rating: int  # 1 or -1
    comment: str = ""
    prompt: str = ""
    correction: str = ""
    model: str = ""


@router.post("/v1/feedback", status_code=201)
async def submit(body: FeedbackBody, ctx: AuthContext = Depends(write)):
    try:
        get_store().record(
            ctx.subject, body.request_id, body.rating, comment=body.comment,
            prompt=body.prompt, correction=body.correction, model=body.model,
        )  # fmt: skip
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"request_id": body.request_id}


@router.get("/v1/feedback")
async def list_feedback(
    ctx: AuthContext = Depends(read), rating: int | None = None, limit: int = 100
):
    s = get_store()
    return {
        "summary": s.summary(ctx.subject),
        "data": s.list(ctx.subject, rating, max(1, min(limit, 1000))),
    }


@router.get("/v1/feedback/eval-tasks")
async def eval_tasks(ctx: AuthContext = Depends(read)):
    """Corrections with prompts, in the task format ``python -m evals`` reads."""
    return {"tasks": get_store().eval_tasks(ctx.subject)}


@router.delete("/v1/feedback/{request_id}")
async def delete_feedback(request_id: str, ctx: AuthContext = Depends(write)):
    if not get_store().delete(ctx.subject, request_id):
        raise HTTPException(404, "Feedback not found.")
    return {"deleted": request_id}
