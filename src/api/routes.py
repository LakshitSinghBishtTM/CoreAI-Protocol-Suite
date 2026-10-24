"""
api/routes.py

Route handlers for CoreAI API v1.
Completions, streaming, agent tasks, provider management, and admin.

Contact: api@coreai.com
"""

import json
import logging
import math
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, status
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from api.auth import AuthContext, require_scope
from coreai.budgets import BudgetExceeded
from coreai.errors import AllProvidersUnavailable, ErrorKind, classify

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1")


# HTTP status and a safe, generic message for each failure kind. Raw provider
# error text is never returned for these: it can contain request details or
# partial credentials. It is logged server-side instead.
_ERROR_RESPONSES = {
    ErrorKind.RATE_LIMIT: (429, "Provider rate limit reached; retry later."),
    ErrorKind.OVERLOADED: (503, "Provider is overloaded; retry later."),
    ErrorKind.TIMEOUT: (504, "Provider timed out."),
    ErrorKind.DEADLINE: (504, "Request deadline exceeded."),
    ErrorKind.CONNECTION: (502, "Could not reach the provider."),
    ErrorKind.SERVER_ERROR: (502, "Provider returned an error."),
    ErrorKind.AUTH: (502, "Provider rejected the gateway's credentials."),
    ErrorKind.QUOTA: (502, "Provider quota is exhausted."),
    ErrorKind.CIRCUIT_OPEN: (503, "Provider temporarily unavailable."),
}


def _retry_after_header(seconds: Optional[float]) -> Optional[Dict[str, str]]:
    if seconds is None:
        return None
    return {"Retry-After": str(max(1, math.ceil(seconds)))}


def _budget_error(exc: BudgetExceeded) -> HTTPException:
    return HTTPException(
        status_code=429,
        detail={
            "error": "budget_exceeded",
            "limit": exc.limit,
            "used": exc.used,
            "cap": exc.cap,
        },
        headers={"Retry-After": str(int(math.ceil(exc.retry_after)))},
    )


def _budget_check(ctx, model: Optional[str] = None) -> None:
    from api.server import get_budgets

    budgets = get_budgets()
    if budgets is not None:
        try:
            budgets.check(ctx.subject)
            budgets.check_model(model)
        except BudgetExceeded as exc:
            raise _budget_error(exc)


def _bind_model_gate(_router) -> None:
    """Let the router enforce per-model caps on the model it actually picks."""
    from api.server import get_budgets

    budgets = get_budgets()
    _router.model_gate = budgets.check_model if budgets is not None else None


def _budget_record(ctx, cost_usd=0.0, tokens=0, model=None) -> None:
    from api.server import get_budgets

    budgets = get_budgets()
    if budgets is not None:
        budgets.record(ctx.subject, cost_usd, tokens)
        budgets.record_model(model, cost_usd, tokens)


def _ledger_record(ctx, provider, model, input_tokens, output_tokens, cost, n=1):
    from api.server import get_usage_ledger

    get_usage_ledger().record(
        ctx.subject, provider, model, input_tokens, output_tokens, cost, requests=n
    )


def _budget_record_stream(ctx, usage) -> None:
    from api.server import get_budgets

    # One ledger row per stream, written when it ends (usage is known then).
    if usage is not None:
        _ledger_record(
            ctx,
            usage.provider,
            usage.model,
            usage.input_tokens,
            usage.output_tokens,
            usage.cost_usd,
        )
    else:
        _ledger_record(ctx, "", "", 0, 0, 0.0)

    budgets = get_budgets()
    if budgets is not None and usage is not None and usage.provider:
        tokens = usage.input_tokens + usage.output_tokens
        budgets.add_usage(ctx.subject, usage.cost_usd, tokens)
        budgets.record_model(usage.model, usage.cost_usd, tokens)


def _http_error(exc: Exception) -> HTTPException:
    """Translate a router/provider exception into the right HTTP response."""
    if isinstance(exc, BudgetExceeded):  # a per-model cap hit while routing
        return _budget_error(exc)
    if isinstance(exc, ValueError):  # our own validation (unknown provider, context)
        return HTTPException(status_code=400, detail=str(exc))

    if isinstance(exc, AllProvidersUnavailable):
        return HTTPException(
            status_code=503,
            detail={
                "error": "all_providers_unavailable",
                "failures": [
                    {"provider": name, "reason": kind.value}
                    for name, kind in exc.failures
                ],
            },
            headers=_retry_after_header(exc.retry_after),
        )

    info = classify(exc)
    if info.kind is ErrorKind.BAD_REQUEST:
        return HTTPException(
            status_code=400, detail=f"Provider rejected the request: {str(exc)[:300]}"
        )
    if info.kind in _ERROR_RESPONSES:
        code, message = _ERROR_RESPONSES[info.kind]
        return HTTPException(
            status_code=code,
            detail=message,
            headers=_retry_after_header(info.retry_after),
        )
    return HTTPException(status_code=500, detail="Internal error.")


# ------------------------------------------------------------------
# Request / response models
# ------------------------------------------------------------------


class Message(BaseModel):
    role: Literal["user", "assistant", "system"] = Field(
        ..., description="user | assistant | system"
    )
    content: str


class CompletionRequest(BaseModel):
    messages: List[Message] = Field(..., min_length=1)
    policy: Optional[str] = Field(
        None, description="Named routing policy (see config/routing.yml)"
    )
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
    available: bool  # False while the circuit breaker is open
    latency_ms: Optional[float] = None  # recent p50, from real requests
    model_list: List[str] = Field(default_factory=list)
    circuit_state: Optional[str] = None  # closed | open | half_open
    health: Optional[float] = None  # 0..1, recent success rate (decayed)
    error_kinds: Dict[str, int] = Field(default_factory=dict)


class ModelInfoResponse(BaseModel):
    model_id: str
    provider: str
    tier: str
    context_window: int
    max_output_tokens: int
    modalities: List[str]
    supports_streaming: bool
    supports_function_calling: bool
    knowledge_cutoff: Optional[str] = None
    input_per_million_usd: Optional[float] = None
    output_per_million_usd: Optional[float] = None


class StatsResponse(BaseModel):
    total_requests: int
    strategy: str
    provider_stats: Dict
    cache_stats: Optional[Dict] = None
    retry_stats: Optional[Dict] = None
    limiter_stats: Optional[Dict] = None
    orchestrator_stats: Optional[Dict] = None
    failovers: int = 0


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
    from api.server import get_router as _get_router
    from providers import CompletionRequest as PCR
    from providers import Message as PM

    if request.stream:
        return await completions_stream(request, ctx)

    _budget_check(ctx, request.model)
    _router = _get_router()
    _bind_model_gate(_router)
    messages = [PM(role=m.role, content=m.content) for m in request.messages]
    preq = PCR(
        messages=messages,
        model=request.model,
        max_tokens=request.max_tokens,
        temperature=request.temperature,
        system_prompt=request.system_prompt,
        metadata={"policy": request.policy} if request.policy else {},
    )

    try:
        resp = await _router.route(preq, preferred_provider=request.provider)
    except Exception as exc:
        logger.error("Completion error for %s: %s", ctx.subject, exc)
        raise _http_error(exc)

    _budget_record(
        ctx,
        resp.cost_usd,
        (resp.input_tokens or 0) + (resp.output_tokens or 0),
        model=resp.model,
    )
    _ledger_record(
        ctx,
        resp.provider,
        resp.model,
        resp.input_tokens,
        resp.output_tokens,
        resp.cost_usd,
    )

    return CompletionResponse(
        content=resp.content,
        model=resp.model,
        provider=resp.provider,
        input_tokens=resp.input_tokens,
        output_tokens=resp.output_tokens,
        cost_usd=resp.cost_usd,
        latency_ms=resp.latency_ms,
        cached=resp.cached,
    )


class ExplainRequest(BaseModel):
    messages: List[Message] = Field(..., min_length=1)
    model: Optional[str] = None
    max_tokens: int = Field(1024, ge=1, le=200_000)
    system_prompt: Optional[str] = None
    provider: Optional[str] = None
    policy: Optional[str] = None


@router.post(
    "/routing/explain",
    summary="Dry run: explain where a request would be routed",
    tags=["routing"],
)
async def explain_routing(
    request: ExplainRequest,
    ctx: AuthContext = Depends(require_scope("completions:write")),
):
    """Nothing is sent to any provider and no routing state changes."""
    from api.server import get_router as _get_router
    from providers import CompletionRequest as PCR
    from providers import Message as PM

    preq = PCR(
        messages=[PM(role=m.role, content=m.content) for m in request.messages],
        model=request.model,
        max_tokens=request.max_tokens,
        system_prompt=request.system_prompt,
    )
    try:
        return _get_router().explain(
            preq, policy_name=request.policy, preferred_provider=request.provider
        )
    except Exception as exc:
        raise _http_error(exc)


def _sse(data: dict) -> str:
    """One Server-Sent Event frame."""
    return f"data: {json.dumps(data)}\n\n"


@router.post(
    "/completions/stream",
    summary="Stream a completion",
    tags=["completions"],
)
async def completions_stream(
    request: CompletionRequest,
    ctx: AuthContext = Depends(require_scope("completions:write")),
):
    from api.server import get_router as _get_router
    from providers import CompletionRequest as PCR
    from providers import Message as PM

    _budget_check(ctx, request.model)
    _router = _get_router()
    _bind_model_gate(_router)
    messages = [PM(role=m.role, content=m.content) for m in request.messages]
    preq = PCR(
        messages=messages,
        model=request.model,
        max_tokens=request.max_tokens,
        temperature=request.temperature,
        system_prompt=request.system_prompt,
        stream=True,
        metadata={"policy": request.policy} if request.policy else {},
    )

    # Open the stream first so failures before any output become proper HTTP
    # errors (with failover, retries and timeouts applied by the router)
    # instead of a 200 response with an error buried in the body.
    stream = _router.stream(preq, preferred_provider=request.provider)
    try:
        first = await stream.__anext__()
    except StopAsyncIteration:
        first = None
    except Exception as exc:
        logger.error("Stream error for %s: %s", ctx.subject, exc)
        raise _http_error(exc)

    # The request is counted now; its cost and tokens are added when the
    # stream ends (see _generate), because they are only known then.
    _budget_record(ctx)

    async def _generate():
        try:
            if first is not None:
                yield _sse({"content": first})
                async for chunk in stream:
                    yield _sse({"content": chunk})
            yield "data: [DONE]\n\n"
        except Exception as exc:
            logger.error("Stream error for %s: %s", ctx.subject, exc)
            yield "event: error\n" + _sse({"error": classify(exc).kind.value})
        finally:
            # Closing the stream lets the router settle it, even if the client
            # left early, so usage is read afterwards.
            await stream.aclose()  # frees the provider's rate-limit slot
            _budget_record_stream(ctx, preq.stream_usage)

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
    from agents.agent_manager import get_agent_manager
    from api.server import get_orchestrator as _get_orch
    from coreai.task_runner import get_task_runner

    orch = _get_orch()
    manager = get_agent_manager()
    runner = get_task_runner()
    if request.agent_id not in manager.agents:
        raise HTTPException(status_code=404, detail="Agent not found")
    if request.agent_id not in orch.agents:  # created before the link existed
        orch.register_agent(request.agent_id)
    try:
        task = orch.assign_task(
            request.agent_id,
            request.objective,
            request.context,
            priority=request.priority,
        )
        task.max_iterations = request.max_iterations
        orch.task_store.update(task)
    except Exception as exc:
        logger.error("Task creation error: %s", exc)
        raise HTTPException(status_code=400, detail=str(exc))
    if runner is not None:
        runner.submit(task.task_id)

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
    from api.server import get_orchestrator as _get_orch

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
    from coreai.task_runner import get_task_runner

    runner = get_task_runner()
    cancelled = runner.cancel(task_id) if runner else False
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
    from api.server import get_orchestrator as _get_orch

    _get_orch().register_agent(
        agent_id, {"name": request.name, "capabilities": request.capabilities}
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
    from coreai.task_runner import get_task_runner

    # Stop the agent's worker and wait for its running task to end *before*
    # the agent is shut down and unregistered.
    runner = get_task_runner()
    if runner is not None:
        await runner.drop_agent(agent_id)
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
    from api.server import get_router as _get_router
    from neural.model import get_registry

    _r = _get_router()
    registry = get_registry()
    results = []
    for name in _r.providers:
        breaker = _r.breakers[name]
        results.append(
            ProviderStatusResponse(
                name=name,
                available=breaker.would_allow(),
                latency_ms=_r._p50_latency(name),
                model_list=[m.model_id for m in registry.list(provider=name)],
                circuit_state=breaker.state.value,
                health=round(_r.health[name].score(), 4),
                error_kinds=dict(_r.provider_stats[name]["error_kinds"]),
            )
        )
    return results


@router.get(
    "/models/details",
    response_model=List[ModelInfoResponse],
    summary="List known models and their capabilities",
    tags=["providers"],
)
async def list_models(
    ctx: AuthContext = Depends(require_scope("completions:read")),
    provider: Optional[str] = Query(None, description="Filter by provider name"),
    modality: Optional[str] = Query(
        None, description="Filter by supported modality, e.g. 'vision'"
    ),
    min_context: Optional[int] = Query(
        None, description="Only models with at least this context window"
    ),
):
    from neural.model import Modality, get_registry

    registry = get_registry()
    modality_enum = None
    if modality:
        try:
            modality_enum = Modality(modality)
        except ValueError:
            valid = [m.value for m in Modality]
            raise HTTPException(
                status_code=422,
                detail=f"Unknown modality '{modality}'. Valid values: {valid}",
            )

    profiles = registry.list(
        provider=provider, modality=modality_enum, min_context=min_context
    )
    return [
        ModelInfoResponse(
            model_id=p.model_id,
            provider=p.provider,
            tier=p.tier.value,
            context_window=p.context_window,
            max_output_tokens=p.max_output_tokens,
            modalities=[m.value for m in p.modalities],
            supports_streaming=p.supports_streaming,
            supports_function_calling=p.supports_function_calling,
            knowledge_cutoff=p.knowledge_cutoff,
            input_per_million_usd=p.pricing.input_per_million if p.pricing else None,
            output_per_million_usd=(
                p.pricing.output_per_million if p.pricing else None
            ),
        )
        for p in profiles
    ]


# ------------------------------------------------------------------
# Stats / admin
# ------------------------------------------------------------------


@router.get(
    "/usage",
    summary="Your usage and limits in the current budget window",
    tags=["completions"],
)
async def get_usage(
    ctx: AuthContext = Depends(require_scope("completions:read")),
):
    from api.server import get_budgets

    budgets = get_budgets()
    status_ = budgets.status(ctx.subject) if budgets else None
    return {"subject": ctx.subject, "budget": status_}


@router.get(
    "/usage/report",
    summary="Usage over time, as JSON or CSV",
    tags=["completions"],
)
async def usage_report(
    since: Optional[float] = Query(None, description="Unix time, inclusive"),
    until: Optional[float] = Query(None, description="Unix time, exclusive"),
    bucket: str = Query("day", description="hour or day"),
    group_by: str = Query("", description="comma list of subject, provider, model"),
    format: str = Query("json", description="json or csv"),
    ctx: AuthContext = Depends(require_scope("completions:read")),
):
    """Your own usage; callers with the ``admin`` scope see every subject."""
    from api.server import get_usage_ledger
    from coreai.usage import UsageQueryError, to_csv

    if format not in ("json", "csv"):
        raise HTTPException(status_code=400, detail="format must be json or csv")
    groups = [g for g in group_by.split(",") if g]
    is_admin = "admin" in ctx.scopes
    if not is_admin and "subject" in groups:
        groups.remove("subject")  # a caller only ever sees themselves
    try:
        rows = get_usage_ledger().report(
            subject=None if is_admin else ctx.subject,
            since=since,
            until=until,
            bucket=bucket,
            group_by=groups,
        )
    except UsageQueryError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if format == "csv":
        return Response(
            to_csv(rows, groups),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="usage.csv"'},
        )
    return {"bucket": bucket, "group_by": groups, "rows": rows}


@router.get(
    "/stats",
    response_model=StatsResponse,
    summary="System statistics",
    tags=["admin"],
)
async def get_stats(
    ctx: AuthContext = Depends(require_scope("admin")),
):
    from api.server import get_orchestrator as _get_orch
    from api.server import get_router as _get_router

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
        failovers=rs.get("failovers", 0),
    )


@router.post(
    "/admin/shutdown",
    summary="Trigger emergency shutdown",
    tags=["admin"],
)
async def trigger_shutdown(
    mode: str = Query("graceful", pattern="^(graceful|immediate|forced)$"),
    message: str = Query("", max_length=256),
    ctx: AuthContext = Depends(require_scope("admin")),
):
    import asyncio
    import os
    import signal

    from agents.agent_manager import get_agent_manager
    from agents.emergency_shutdown import (
        EmergencyShutdown,
        ShutdownMode,
        ShutdownReason,
    )
    from api.server import app_state, drain, get_audit
    from coreai.kernel import get_kernel
    from database.db import get_db

    # One shared instance: a second request must see the first one running.
    shutdown = app_state.get("shutdown")
    if shutdown is not None and shutdown.is_shutting_down:
        get_audit().record(
            "admin.shutdown",
            actor=ctx.subject,
            outcome="rejected",
            details={"mode": mode, "reason": "already_shutting_down"},
        )
        raise HTTPException(
            status_code=409,
            detail={"error": "already_shutting_down", "event": shutdown.last_event()},
        )
    shutdown = EmergencyShutdown(
        agent_manager=get_agent_manager(),
        kernel=get_kernel(),
        db=get_db(),
        drain=drain,
        task_runner=app_state.get("task_runner"),
        # Ask the server to exit cleanly (uvicorn finishes its own shutdown)
        # rather than raising SystemExit inside a background task.
        exit_fn=lambda code: os.kill(os.getpid(), signal.SIGTERM),
    )
    app_state["shutdown"] = shutdown
    get_audit().record(
        "admin.shutdown",
        actor=ctx.subject,
        details={"mode": mode, "message": message},
    )
    # Keep a reference so the task isn't garbage-collected mid-shutdown.
    app_state["shutdown_task"] = asyncio.create_task(
        shutdown.trigger(
            mode=ShutdownMode(mode),
            reason=ShutdownReason.API_TRIGGER,
            triggered_by=ctx.subject,
            message=message,
        )
    )
    return {"status": "shutdown_initiated", "mode": mode, "by": ctx.subject}


@router.get(
    "/admin/policies",
    summary="Active routing policies (read-only)",
    tags=["admin"],
)
async def list_policies(ctx: AuthContext = Depends(require_scope("admin"))):
    from api.server import get_router as _get_router

    store = _get_router().policies
    if store is None:
        return {"policies": [], "loaded": False}
    current = store.current()
    return {
        "loaded": True,
        "last_error": store.last_error,
        "reloads": store.reloads,
        "policies": [current.policies[n].to_dict() for n in current.names()],
    }


@router.get(
    "/admin/budgets",
    summary="Budget limits and current usage for the caller or a subject",
    tags=["admin"],
)
async def get_budget_status(
    subject: Optional[str] = Query(None, max_length=128),
    ctx: AuthContext = Depends(require_scope("admin")),
):
    from api.server import app_state

    manager = app_state.get("budgets")
    if manager is None:
        return {"enabled": False}
    target = subject or ctx.subject
    return {
        "enabled": True,
        "subject": target,
        "status": manager.status(target),
        "stats": manager.stats(),
    }


class BudgetLimitsBody(BaseModel):
    max_cost_usd: Optional[float] = None
    warn_cost_usd: Optional[float] = None
    max_requests: Optional[int] = None
    max_tokens: Optional[int] = None
    window_seconds: int = 86400


@router.put(
    "/admin/budgets/{subject}",
    summary="Set budget limits for a subject (in memory until restart)",
    tags=["admin"],
)
async def set_budget(
    subject: str = Path(..., max_length=128),
    body: BudgetLimitsBody = Body(...),
    ctx: AuthContext = Depends(require_scope("admin")),
):
    from api.server import app_state, get_audit
    from coreai.budgets import BudgetLimits

    manager = app_state.get("budgets")
    if manager is None:
        raise HTTPException(status_code=409, detail="budgets are not enabled")
    try:
        limits = BudgetLimits(**body.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    manager.set_limits(subject, limits)
    get_audit().record("budget.set", ctx.subject, subject, details=body.model_dump())
    return {"subject": subject, "status": manager.status(subject)}


@router.delete(
    "/admin/budgets/{subject}",
    summary="Remove a subject's budget override (falls back to the default)",
    tags=["admin"],
)
async def remove_budget(
    subject: str = Path(..., max_length=128),
    ctx: AuthContext = Depends(require_scope("admin")),
):
    from api.server import app_state, get_audit

    manager = app_state.get("budgets")
    if manager is None:
        raise HTTPException(status_code=409, detail="budgets are not enabled")
    removed = manager.remove_limits(subject)
    get_audit().record("budget.remove", ctx.subject, subject, outcome="ok" if removed else "noop")
    return {"subject": subject, "removed": removed}


@router.get(
    "/admin/audit",
    summary="Recent audit events, newest first",
    tags=["admin"],
)
async def get_audit_events(
    limit: int = Query(100, ge=1, le=1000),
    action: Optional[str] = Query(None, max_length=64),
    actor: Optional[str] = Query(None, max_length=128),
    ctx: AuthContext = Depends(require_scope("admin")),
):
    from api.server import get_audit

    return {"events": get_audit().events(limit=limit, action=action, actor=actor)}


@router.get(
    "/admin/shutdown",
    summary="Kill-switch status",
    tags=["admin"],
)
async def shutdown_status(ctx: AuthContext = Depends(require_scope("admin"))):
    from api.server import app_state, drain

    shutdown = app_state.get("shutdown")
    return {
        **drain.snapshot(),
        "event": shutdown.last_event() if shutdown else None,
    }


# ------------------------------------------------------------------
# Fine-tuning (neural/training.py)
#
# Only "openai" has a real provider-side adapter (providers/openai.py's
# fine_tuning_client()) -- the AsyncOpenAI SDK genuinely has a
# fine-tuning API. neural/training.py's own module docstring says
# Anthropic support is "planned", not built, so that path returns a
# clear 501 rather than pretending it works.
# ------------------------------------------------------------------


class TrainingExampleRequest(BaseModel):
    messages: List[Dict[str, str]]
    weight: float = 1.0


class SubmitFineTuneRequest(BaseModel):
    base_model: str
    provider: str = "openai"
    examples: List[TrainingExampleRequest]
    hyperparam_preset: str = Field(
        "balanced", description="conservative | balanced | aggressive"
    )
    suffix: str = "coreai-ft"
    description: str = ""
    max_cost_usd: Optional[float] = Field(
        None,
        ge=0,
        description="Refuse (before anything is uploaded) if the pre-launch estimate is higher.",
    )


class FineTuneJobResponse(BaseModel):
    job_id: str
    status: str
    fine_tuned_model: Optional[str] = None
    base_model: Optional[str] = None
    provider: Optional[str] = None
    trained_tokens: int
    duration_s: Optional[float] = None
    cost_estimate_usd: float
    estimated_usd: Optional[float] = None
    checkpoint_count: int
    error: Optional[str] = None


def _finetune_provider(provider_name: str):
    from api.server import get_router as _get_router

    provider = _get_router().providers.get(provider_name)
    if provider is None or not hasattr(provider, "fine_tuning_client"):
        raise HTTPException(
            status_code=501,
            detail=(
                f"Fine-tuning for provider '{provider_name}' is not "
                f"available. Only 'openai' has a real fine-tuning adapter "
                f"wired up; neural/training.py's own docs mark Anthropic "
                f"support as planned, not built."
            ),
        )
    return provider


def _finetune_config(body: "SubmitFineTuneRequest"):
    from neural.training import (
        HyperparamConfig,
        HyperparamPreset,
        TrainingDataset,
        TrainingExample,
        TrainingJobConfig,
    )

    try:
        preset = HyperparamPreset(body.hyperparam_preset)
    except ValueError:
        valid = [p.value for p in HyperparamPreset]
        raise HTTPException(
            status_code=422,
            detail=f"Unknown hyperparam_preset '{body.hyperparam_preset}'. Valid: {valid}",
        )
    dataset = TrainingDataset(
        examples=[
            TrainingExample(messages=e.messages, weight=e.weight) for e in body.examples
        ]
    )
    return TrainingJobConfig(
        base_model=body.base_model,
        provider=body.provider,
        dataset=dataset,
        hyperparams=HyperparamConfig.from_preset(preset),
        suffix=body.suffix,
        description=body.description,
    )


@router.post(
    "/finetune/estimate",
    summary="Estimate a fine-tuning job's cost without submitting it",
    tags=["admin"],
)
async def estimate_finetune_job(
    body: SubmitFineTuneRequest,
    ctx: AuthContext = Depends(require_scope("admin")),
):
    from neural.training import get_training_manager

    config = _finetune_config(body)
    validation = config.dataset.validate()
    return {"validation": validation, **get_training_manager().estimate(config)}


@router.post(
    "/finetune/jobs",
    response_model=FineTuneJobResponse,
    status_code=201,
    summary="Submit a fine-tuning job",
    tags=["admin"],
)
async def submit_finetune_job(
    body: SubmitFineTuneRequest,
    ctx: AuthContext = Depends(require_scope("admin")),
):
    from neural.training import (
        CostLimitError,
        DatasetValidationError,
        TrainingError,
        UnsupportedProviderError,
        get_training_manager,
    )

    provider = _finetune_provider(body.provider)
    config = _finetune_config(body)

    try:
        job = await get_training_manager().submit(
            config, provider.fine_tuning_client(), max_cost_usd=body.max_cost_usd
        )
    except (DatasetValidationError, CostLimitError) as e:
        raise HTTPException(status_code=422, detail=str(e))
    except UnsupportedProviderError as e:
        raise HTTPException(status_code=501, detail=str(e))
    except TrainingError as e:
        raise HTTPException(status_code=502, detail=str(e))

    return FineTuneJobResponse(**job.to_dict())


@router.get(
    "/finetune/jobs",
    response_model=List[FineTuneJobResponse],
    summary="List fine-tuning jobs",
    tags=["admin"],
)
async def list_finetune_jobs(
    ctx: AuthContext = Depends(require_scope("admin")),
    status: Optional[str] = Query(None, description="Filter by job status"),
):
    from neural.training import TrainingStatus, get_training_manager

    status_enum = None
    if status:
        try:
            status_enum = TrainingStatus(status)
        except ValueError:
            valid = [s.value for s in TrainingStatus]
            raise HTTPException(
                status_code=422, detail=f"Unknown status '{status}'. Valid: {valid}"
            )
    jobs = get_training_manager().list_jobs(status=status_enum)
    return [FineTuneJobResponse(**j.to_dict()) for j in jobs]


@router.get(
    "/finetune/jobs/{job_id}",
    response_model=FineTuneJobResponse,
    summary="Get fine-tuning job status",
    tags=["admin"],
)
async def get_finetune_job(
    job_id: str,
    ctx: AuthContext = Depends(require_scope("admin")),
):
    from neural.training import TrainingError, TrainingStatus, get_training_manager

    manager = get_training_manager()
    job = manager.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown fine-tune job")

    terminal = (
        TrainingStatus.SUCCEEDED,
        TrainingStatus.FAILED,
        TrainingStatus.CANCELLED,
    )
    if job.status not in terminal and job.config is not None:
        provider = _finetune_provider(job.config.provider)
        try:
            job = await manager.poll(job_id, provider.fine_tuning_client())
        except TrainingError as e:
            raise HTTPException(status_code=502, detail=str(e))

    return FineTuneJobResponse(**job.to_dict())


@router.delete(
    "/finetune/jobs/{job_id}",
    status_code=204,
    summary="Cancel a fine-tuning job",
    tags=["admin"],
)
async def cancel_finetune_job(
    job_id: str,
    ctx: AuthContext = Depends(require_scope("admin")),
):
    from neural.training import TrainingError, get_training_manager

    manager = get_training_manager()
    job = manager.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown fine-tune job")
    if job.config is None:
        raise HTTPException(
            status_code=409, detail="Job has no associated provider config"
        )

    provider = _finetune_provider(job.config.provider)
    try:
        await manager.cancel(job_id, provider.fine_tuning_client())
    except TrainingError as e:
        raise HTTPException(status_code=409, detail=str(e))
