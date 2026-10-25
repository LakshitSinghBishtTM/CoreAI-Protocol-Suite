"""api/runs_api.py: HTTP surface of the agent harness (start, status, control, stream, kill)."""

import json
from types import SimpleNamespace
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
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


@router.get("/v1/harness/agents", summary="Agents defined in config/agents.yml")
async def list_agents(
    ctx: AuthContext = Depends(require_scope("runs:read")), rt=Depends(get_harness)
):
    return {
        "object": "list",
        "data": [
            {
                "name": a.name,
                "allowed_tools": list(a.allowed_tools),
                "approval_tools": list(a.approval_tools),
                "max_steps": a.max_steps,
                "max_cost_usd": a.max_cost_usd,
                "model": a.model,
            }
            for a in rt.runner.agents.values()
        ],
    }


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


class RunWorkflow(BaseModel):
    input: str = Field(default="", max_length=50_000)
    approved: list[str] = Field(
        default_factory=list, description="step ids a human approved"
    )


@router.get("/v1/workflows", summary="Configured fixed-step workflows")
async def list_workflows(
    ctx: AuthContext = Depends(require_scope("runs:read")), rt=Depends(get_harness)
):
    return {
        "object": "list",
        "data": [
            {
                "name": n,
                "steps": [
                    {"id": s.id, "kind": s.kind, "tool": s.tool or None}
                    for s in w["steps"]
                ],
                "max_cost_usd": w["max_cost_usd"],
            }
            for n, w in sorted(rt.workflows.items())
        ],
    }


@router.post("/v1/workflows/{name}/run", summary="Run a fixed-step workflow")
async def run_workflow_endpoint(
    name: str,
    body: RunWorkflow,
    ctx: AuthContext = Depends(require_scope("runs:write")),
    rt=Depends(get_harness),
):
    """Runs synchronously. Tools must be granted to the caller, as for agent runs.
    Model calls go through the gateway with the caller's budget. The result is
    recorded in this workflow's history (`GET /v1/workflows/{name}/runs`)."""
    from api.server import app_state
    from harness import run_workflow

    wf = rt.workflows.get(name)
    if wf is None:
        raise HTTPException(status_code=404, detail="Unknown workflow.")
    tools_rt = app_state.get("tools")
    granted = (
        {t.name for t in tools_rt.registry.visible(tools_rt.allowed_for(ctx.subject))}
        if tools_rt
        else set()
    )
    needed = {s.tool for s in wf["steps"] if s.kind == "tool"}
    missing = sorted(needed - granted)
    if missing:
        raise HTTPException(
            status_code=403, detail=f"Tools not granted to caller: {missing}"
        )
    unknown = sorted(set(body.approved) - {s.id for s in wf["steps"]})
    if unknown:
        raise HTTPException(status_code=422, detail=f"Unknown step ids: {unknown}")
    if body.approved and "admin" not in ctx.scopes:
        raise HTTPException(status_code=403, detail="Only an admin can approve steps.")

    async def route(req):
        req.metadata = {**(req.metadata or {}), "owner": ctx.subject}
        return await rt.runner.route(req)

    out = await run_workflow(
        wf["steps"], rt.runner.registry, route, input=body.input,
        allowed=sorted(needed), caller=ctx.subject, approved=set(body.approved),
        max_cost_usd=wf["max_cost_usd"],
    )  # fmt: skip
    out["run_id"] = rt.runner.store.record_workflow(name, ctx.subject, body.input, out)
    return out


@router.get(
    "/v1/workflows/{name}/runs", summary="Past runs of one workflow, newest first"
)
async def list_workflow_runs(
    name: str,
    limit: int = Query(50, ge=1, le=500),
    ctx: AuthContext = Depends(require_scope("runs:read")),
    rt=Depends(get_harness),
):
    if name not in rt.workflows:
        raise HTTPException(status_code=404, detail="Unknown workflow.")
    rows = rt.runner.store.list_workflow_runs(name, limit)
    if "admin" not in ctx.scopes:  # callers see their own runs only
        rows = [r for r in rows if r.get("caller") == ctx.subject]
    return {"object": "list", "data": rows}


@router.get("/v1/workflows/{name}/runs/{run_id}", summary="One stored workflow run")
async def get_workflow_run(
    name: str,
    run_id: str,
    ctx: AuthContext = Depends(require_scope("runs:read")),
    rt=Depends(get_harness),
):
    if name not in rt.workflows:
        raise HTTPException(status_code=404, detail="Unknown workflow.")
    run = rt.runner.store.get_workflow_run(run_id)
    if run is None or run.get("workflow") != name:
        raise HTTPException(status_code=404, detail="Run not found.")
    if run.get("caller") != ctx.subject and "admin" not in ctx.scopes:
        raise HTTPException(status_code=404, detail="Run not found.")
    return run


@router.post(
    "/v1/triggers/{name}/fire",
    status_code=202,
    summary="Signed inbound webhook trigger",
)
async def fire_trigger(name: str, request: Request, rt=Depends(get_harness)):
    """No bearer token: the HMAC signature is the credential (same scheme as
    outbound webhooks). Unknown, non-webhook and bad-signature all look alike."""
    tm = rt.triggers
    content_length = request.headers.get("content-length")
    try:
        if content_length is not None and int(content_length) > 64_000:
            raise HTTPException(status_code=413, detail="Body too large.")
    except ValueError:
        # Let the ASGI server validate malformed framing headers.
        pass
    # Bound memory use while receiving an unauthenticated request. Checking
    # len(await request.body()) only rejects after Starlette has buffered the
    # complete body, which lets a signed-trigger endpoint be used for memory
    # exhaustion with arbitrarily large payloads.
    body = bytearray()
    too_large = False
    async for chunk in request.stream():
        if too_large or len(body) + len(chunk) > 64_000:
            # Drain without retaining further data so the ASGI server can
            # finish the request cleanly before we send the rejection.
            too_large = True
        else:
            body.extend(chunk)
    if too_large:
        raise HTTPException(status_code=413, detail="Body too large.")
    body = bytes(body)
    try:
        started = (
            tm.fire_webhook(
                name,
                body,
                request.headers.get("X-CaiPS-Timestamp", ""),
                request.headers.get("X-CaiPS-Signature", ""),
            )
            if tm
            else (_ for _ in ()).throw(KeyError(name))
        )
    except (KeyError, PermissionError):
        raise HTTPException(status_code=401, detail="Invalid trigger or signature.")
    return {"started": started}
