"""api/runs_api.py: HTTP surface of the agent harness (start, status, control, stream, kill)."""

import json
from types import SimpleNamespace
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from api.auth import AuthContext, require_scope

router = APIRouter(tags=["agents"])
TERMINAL = ("completed", "failed", "cancelled")


def get_harness():
    from api.server import app_state

    rt = app_state.get("harness")
    if rt is None:
        raise HTTPException(status_code=404, detail="Agent harness is not enabled.")
    return rt


def _own(rt, rid: str, ctx: AuthContext) -> dict:
    run = rt.runner.status(rid)
    if run is None or (run["owner"] != ctx.subject and "admin" not in ctx.scopes):
        raise HTTPException(
            status_code=404, detail="Run not found."
        )  # no existence leak
    return run


def gateway_route(router_, runner_tools_rt=None):
    """Route harness model calls through the gateway with the owner's budget:
    checked before every call, recorded after."""
    from api import routes

    async def route(req):
        ctx = SimpleNamespace(subject=req.metadata.get("owner", "anonymous"))
        routes._budget_check(ctx, req.model)
        resp = await router_.route(req)
        routes._budget_record(
            ctx,
            resp.cost_usd,
            (resp.input_tokens or 0) + (resp.output_tokens or 0),
            model=resp.model,
        )
        routes._ledger_record(
            ctx,
            resp.provider,
            resp.model,
            resp.input_tokens,
            resp.output_tokens,
            resp.cost_usd,
        )
        return resp

    return route


class StartRun(BaseModel):
    agent: str
    task: str = Field(min_length=1, max_length=50_000)


class Decision(BaseModel):
    approve: Optional[bool] = None


class Kill(BaseModel):
    owner: Optional[str] = None


@router.post("/v1/runs", status_code=202, summary="Start an agent run")
async def start_run(
    body: StartRun,
    ctx: AuthContext = Depends(require_scope("runs:write")),
    rt=Depends(get_harness),
):
    from api.server import app_state

    agent = rt.runner.agents.get(body.agent)
    if agent is None:
        raise HTTPException(status_code=404, detail="Unknown agent.")
    tools_rt = app_state.get("tools")
    granted = (
        {t.name for t in tools_rt.registry.visible(tools_rt.allowed_for(ctx.subject))}
        if tools_rt
        else set()
    )
    missing = sorted(set(agent.allowed_tools) - granted)
    if missing:
        raise HTTPException(
            status_code=403, detail=f"Tools not granted to caller: {missing}"
        )
    rid = rt.runner.create(body.agent, body.task, owner=ctx.subject)
    rt.spawn(rid)
    return rt.runner.status(rid)


@router.get("/v1/runs", summary="Runs the caller may see")
async def list_runs(
    ctx: AuthContext = Depends(require_scope("runs:read")), rt=Depends(get_harness)
):
    owner = None if "admin" in ctx.scopes else ctx.subject
    return {"object": "list", "data": rt.runner.store.list_runs(owner=owner)}


@router.post(
    "/v1/runs/kill",
    summary="Kill switch: stop all active runs for an owner, or everything",
)
async def kill_runs(
    body: Kill,
    ctx: AuthContext = Depends(require_scope("admin")),
    rt=Depends(get_harness),
):
    n = rt.runner.kill(body.owner)
    return {"killed": n, "owner": body.owner}


@router.get("/v1/runs/{rid}", summary="Run status")
async def get_run(
    rid: str,
    ctx: AuthContext = Depends(require_scope("runs:read")),
    rt=Depends(get_harness),
):
    return _own(rt, rid, ctx)


@router.get(
    "/v1/runs/{rid}/events", summary="Trace events, optionally after a sequence number"
)
async def run_events(
    rid: str,
    after: int = 0,
    ctx: AuthContext = Depends(require_scope("runs:read")),
    rt=Depends(get_harness),
):
    _own(rt, rid, ctx)
    return {"object": "list", "data": rt.runner.store.events(rid, after)}


@router.get(
    "/v1/runs/{rid}/stream", summary="Server-sent events until the run ends or blocks"
)
async def stream_run(
    rid: str,
    ctx: AuthContext = Depends(require_scope("runs:read")),
    rt=Depends(get_harness),
):
    _own(rt, rid, ctx)

    async def gen():
        async for e in rt.runner.stream(rid):
            yield f"data: {json.dumps(e)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.post("/v1/runs/{rid}/cancel", summary="Cancel a run")
async def cancel_run(
    rid: str,
    ctx: AuthContext = Depends(require_scope("runs:write")),
    rt=Depends(get_harness),
):
    _own(rt, rid, ctx)
    return {"cancelled": rt.runner.cancel(rid)}


@router.post("/v1/runs/{rid}/pause", summary="Pause a run after its current step")
async def pause_run(
    rid: str,
    ctx: AuthContext = Depends(require_scope("runs:write")),
    rt=Depends(get_harness),
):
    _own(rt, rid, ctx)
    return {"pausing": rt.runner.pause(rid)}


@router.post(
    "/v1/runs/{rid}/resume",
    summary="Resume a paused run, or answer an approval request",
)
async def resume_run(
    rid: str,
    body: Decision,
    ctx: AuthContext = Depends(require_scope("runs:write")),
    rt=Depends(get_harness),
):
    run = _own(rt, rid, ctx)
    if run["status"] == "waiting_approval":
        if body.approve is None:
            raise HTTPException(
                status_code=400, detail="This run needs approve: true or false."
            )
        if (
            "admin" not in ctx.scopes
        ):  # the requester must not approve their own destructive call
            raise HTTPException(
                status_code=403, detail="Approvals need the admin scope."
            )
    elif run["status"] not in ("paused", "failed"):
        raise HTTPException(status_code=409, detail=f"Run is {run['status']}.")
    rt.spawn(
        rid, decision=body.approve if run["status"] == "waiting_approval" else None
    )
    return rt.runner.status(rid)
