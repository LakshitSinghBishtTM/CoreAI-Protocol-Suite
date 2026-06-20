"""
api/routes.py

Route handlers for CoreAI API v1.
Completions, streaming, agent tasks, provider management, and admin.

Contact: api@coreai.com
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from api.auth import AuthContext, require_scope

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1")


# ------------------------------------------------------------------
# Request / response models
# ------------------------------------------------------------------


class Message(BaseModel):
    role: str = Field(..., description="user | assistant | system")
    content: str


class CompletionRequest(BaseModel):
    messages: List[Message]
    model: Optional[str] = None
    max_tokens: int = Field(1024, ge=1, le=32768)
    temperature: float = Field(0.7, ge=0.0, le=2.0)
    system_prompt: Optional[str] = None
    provider: Optional[str] = None
    stream: bool = False


class CompletionResponse(BaseModel):
    content: str
    model: str
    provider: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: float
    cached: bool = False
    request_id: Optional[str] = None


class TaskRequest(BaseModel):
    agent_id: str
    objective: str
    context: Dict[str, Any] = Field(default_factory=dict)
    max_iterations: int = Field(10, ge=1, le=50)
    priority: int = Field(2, ge=0, le=3)


class TaskResponse(BaseModel):
    task_id: str
    agent_id: str
    objective: str
    status: str
    result: Optional[str] = None
    error: Optional[str] = None
    iterations: int
    created_at: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None


class AgentRegisterRequest(BaseModel):
    name: str
    capabilities: List[str] = Field(default_factory=list)
    config: Dict[str, Any] = Field(default_factory=dict)


class ProviderStatusResponse(BaseModel):
    name: str
    available: bool
    latency_ms: Optional[float] = None
    model_list: List[str] = Field(default_factory=list)


class StatsResponse(BaseModel):
    total_requests: int
    strategy: str
    provider_stats: Dict
    cache_stats: Optional[Dict] = None
    retry_stats: Optional[Dict] = None
    limiter_stats: Optional[Dict] = None
    orchestrator_stats: Optional[Dict] = None


# ------------------------------------------------------------------
# Completions
# ------------------------------------------------------------------


@router.post(
    "/completions",
    response_model=CompletionResponse,
    summary="Generate a completion",
    tags=["completions"],
)
async def completions(
    request: CompletionRequest,
    ctx: AuthContext = Depends(require_scope("completions:write")),
):
    from coreai.router import get_router as _get_router
    from providers import CompletionRequest as PCR
    from providers import Message as PM

    _router = _get_router()
    messages = [PM(role=m.role, content=m.content) for m in request.messages]
    preq = PCR(
        messages=messages,
        model=request.model,
        max_tokens=request.max_tokens,
        temperature=request.temperature,
        system_prompt=request.system_prompt,
    )

    try:
        resp = await _router.route(preq, preferred_provider=request.provider)
    except Exception as exc:
        logger.error("Completion error for %s: %s", ctx.subject, exc)
        raise HTTPException(status_code=500, detail=str(exc))

    return CompletionResponse(
        content=resp.content,
        model=resp.model,
        provider=resp.provider,
        input_tokens=resp.input_tokens,
        output_tokens=resp.output_tokens,
        cost_usd=resp.cost_usd,
        latency_ms=resp.latency_ms,
    )


@router.post(
    "/completions/stream",
    summary="Stream a completion",
    tags=["completions"],
)
async def completions_stream(
    request: CompletionRequest,
    ctx: AuthContext = Depends(require_scope("completions:write")),
):
    from coreai.router import get_router as _get_router
    from providers import CompletionRequest as PCR
    from providers import Message as PM

    _router = _get_router()
    messages = [PM(role=m.role, content=m.content) for m in request.messages]
    preq = PCR(
        messages=messages,
        model=request.model,
        max_tokens=request.max_tokens,
        temperature=request.temperature,
        system_prompt=request.system_prompt,
        stream=True,
    )

    provider_name = request.provider or next(iter(_router.providers))
    provider = _router.providers.get(provider_name)
    if not provider:
        raise HTTPException(
            status_code=400, detail=f"Unknown provider: {provider_name}"
        )

    async def _generate():
        try:
            async for chunk in provider.stream(preq):
                yield chunk
        except Exception as exc:
            logger.error("Stream error for %s: %s", ctx.subject, exc)
            yield f"[stream_error] {exc}"

    return StreamingResponse(_generate(), media_type="text/event-stream")


# ------------------------------------------------------------------
# Agent tasks
# ------------------------------------------------------------------


@router.post(
    "/tasks",
    response_model=TaskResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Submit a task to an agent",
    tags=["tasks"],
)
async def create_task(
    request: TaskRequest,
    ctx: AuthContext = Depends(require_scope("tasks:write")),
):
    from coreai.orchestrator import get_orchestrator as _get_orch

    orch = _get_orch()
    try:
        task = orch.assign_task(
            request.agent_id,
            request.objective,
            request.context,
        )
        task.max_iterations = request.max_iterations
        orch.task_store.update(task)
    except Exception as exc:
        logger.error("Task creation error: %s", exc)
        raise HTTPException(status_code=400, detail=str(exc))

    return TaskResponse(
        task_id=task.task_id,
        agent_id=task.agent_id,
        objective=task.objective,
        status=task.status.value,
        iterations=task.iterations,
        created_at=task.created_at.isoformat(),
    )


@router.get(
    "/tasks/{task_id}",
    response_model=TaskResponse,
    summary="Get task status",
    tags=["tasks"],
)
async def get_task(
    task_id: str = Path(..., description="Task ID"),
    ctx: AuthContext = Depends(require_scope("tasks:read")),
):
    from coreai.orchestrator import get_orchestrator as _get_orch

    task = _get_orch().get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    return TaskResponse(
        task_id=task.task_id,
        agent_id=task.agent_id,
        objective=task.objective,
        status=task.status.value,
        result=task.result,
        error=task.error,
        iterations=task.iterations,
        created_at=task.created_at.isoformat(),
        started_at=task.started_at.isoformat() if task.started_at else None,
        completed_at=task.completed_at.isoformat() if task.completed_at else None,
    )


@router.delete(
    "/tasks/{task_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Cancel a task",
    tags=["tasks"],
)
async def cancel_task(
    task_id: str = Path(...),
    ctx: AuthContext = Depends(require_scope("tasks:write")),
):
    from coreai.orchestrator import get_orchestrator as _get_orch

    cancelled = _get_orch().cancel_task(task_id)
    if not cancelled:
        raise HTTPException(
            status_code=404, detail="Task not found or already completed"
        )


# ------------------------------------------------------------------
# Agents
# ------------------------------------------------------------------


@router.post(
    "/agents",
    status_code=status.HTTP_201_CREATED,
    summary="Register a new agent",
    tags=["agents"],
)
async def register_agent(
    request: AgentRegisterRequest,
    ctx: AuthContext = Depends(require_scope("agents:write")),
):
    from agents.agent_manager import get_agent_manager

    manager = get_agent_manager()
    agent_id = await manager.spawn_agent(
        name=request.name,
        capabilities=request.capabilities,
        config=request.config,
    )
    return {"agent_id": agent_id, "status": "registered"}


@router.get(
    "/agents",
    summary="List all agents",
    tags=["agents"],
)
async def list_agents(
    ctx: AuthContext = Depends(require_scope("agents:read")),
):
    from agents.agent_manager import get_agent_manager

    return get_agent_manager().list_agents()


@router.delete(
    "/agents/{agent_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Terminate an agent",
    tags=["agents"],
)
async def terminate_agent(
    agent_id: str = Path(...),
    ctx: AuthContext = Depends(require_scope("agents:write")),
):
    from agents.agent_manager import get_agent_manager

    await get_agent_manager().terminate_agent(agent_id, reason=f"api:{ctx.subject}")


# ------------------------------------------------------------------
# Providers
# ------------------------------------------------------------------


@router.get(
    "/providers",
    response_model=List[ProviderStatusResponse],
    summary="List provider health",
    tags=["providers"],
)
async def list_providers(
    ctx: AuthContext = Depends(require_scope("completions:read")),
):
    from coreai.router import get_router as _get_router

    results = []
    for name, provider in _get_router().providers.items():
        try:
            ping_ms = await provider.ping()
            results.append(
                ProviderStatusResponse(
                    name=name,
                    available=True,
                    latency_ms=ping_ms,
                    model_list=provider.available_models(),
                )
            )
        except Exception:
            results.append(ProviderStatusResponse(name=name, available=False))
    return results


# ------------------------------------------------------------------
# Stats / admin
# ------------------------------------------------------------------


@router.get(
    "/stats",
    response_model=StatsResponse,
    summary="System statistics",
    tags=["admin"],
)
async def get_stats(
    ctx: AuthContext = Depends(require_scope("admin")),
):
    from coreai.orchestrator import get_orchestrator as _get_orch
    from coreai.router import get_router as _get_router

    rs = _get_router().stats()
    os_ = _get_orch().stats()

    return StatsResponse(
        total_requests=rs["total_requests"],
        strategy=rs["strategy"],
        provider_stats=rs["provider_stats"],
        cache_stats=rs.get("cache_stats"),
        retry_stats=rs.get("retry_stats"),
        limiter_stats=rs.get("limiter_stats"),
        orchestrator_stats=os_,
    )


@router.post(
    "/admin/shutdown",
    summary="Trigger emergency shutdown",
    tags=["admin"],
)
async def trigger_shutdown(
    mode: str = Query("graceful", regex="^(graceful|immediate|forced)$"),
    message: str = Query("", max_length=256),
    ctx: AuthContext = Depends(require_scope("admin")),
):
    from agents.emergency_shutdown import (
        EmergencyShutdown,
        ShutdownMode,
        ShutdownReason,
    )
    from coreai.kernel import get_kernel
    from database.db import get_db

    shutdown = EmergencyShutdown(
        agent_manager=None,
        kernel=get_kernel(),
        db=get_db(),
    )
    import asyncio

    asyncio.create_task(
        shutdown.trigger(
            mode=ShutdownMode(mode),
            reason=ShutdownReason.API_TRIGGER,
            triggered_by=ctx.subject,
            message=message,
        )
    )
    return {"status": "shutdown_initiated", "mode": mode, "by": ctx.subject}
