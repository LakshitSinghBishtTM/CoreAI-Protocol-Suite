"""api/tools_api.py

HTTP surface of the tools layer: MCP endpoint, tool listing and approvals.
Which tools a caller may use comes from server-side config (grants), never
from the request.
"""

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from api.auth import AuthContext, require_scope

router = APIRouter(tags=["tools"])


def get_tools_runtime():
    from api.server import app_state

    rt = app_state.get("tools")
    if rt is None or not rt.enabled:
        raise HTTPException(status_code=404, detail="Tools layer is not enabled.")
    return rt


@router.get("/v1/tools", summary="Tools the caller may use")
async def list_tools(
    ctx: AuthContext = Depends(require_scope("tools:read")),
    rt=Depends(get_tools_runtime),
):
    tools = rt.registry.visible(rt.allowed_for(ctx.subject))
    return {
        "object": "list",
        "data": [
            {
                "name": t.name, "description": t.description, "version": t.version,
                "owner": t.owner, "permission": t.permission.name.lower(),
                "parameters": t.parameters,
            }
            for t in tools
        ],
    }  # fmt: skip


@router.post("/mcp", summary="MCP endpoint (JSON-RPC 2.0)")
async def mcp_endpoint(
    body: Any = Body(...),
    ctx: AuthContext = Depends(require_scope("tools:call")),
    rt=Depends(get_tools_runtime),
):
    reply = await rt.mcp.handle(body, ctx.subject)
    if reply is None:
        return Response(status_code=202)
    return JSONResponse(reply)


@router.get("/v1/tools/approvals", summary="Pending tool approvals")
async def pending_approvals(
    ctx: AuthContext = Depends(require_scope("admin")),
    rt=Depends(get_tools_runtime),
):
    return {"object": "list", "data": [a.public() for a in rt.approvals.pending()]}


class Decision(BaseModel):
    approve: bool


@router.post("/v1/tools/approvals/{approval_id}", summary="Approve or deny a tool call")
async def decide_approval(
    approval_id: str,
    body: Decision,
    ctx: AuthContext = Depends(require_scope("admin")),
    rt=Depends(get_tools_runtime),
):
    try:
        a = rt.approvals.decide(approval_id, body.approve, by=ctx.subject)
    except KeyError:
        raise HTTPException(status_code=404, detail="No such approval.")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    from api.server import get_audit

    get_audit().record(
        "tool.approval", actor=ctx.subject, target=a.id,
        outcome=a.status, details={"tool": a.tool, "caller": a.caller},
    )  # fmt: skip
    return a.public()
