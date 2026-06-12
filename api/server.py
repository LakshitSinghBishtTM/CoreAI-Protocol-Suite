import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from loguru import logger

from providers import load_providers, Message as ProviderMessage, CompletionRequest as ProviderCompletionRequest
from coreai import Router, RoutingConfig, RoutingStrategy, Orchestrator, TaskStatus


# ============================================================================
# Models
# ============================================================================


class Message(BaseModel):
    role: str = Field(..., description="Message role: user, assistant, system")
    content: str = Field(..., description="Message content")


class CompletionRequest(BaseModel):
    messages: list[Message] = Field(..., description="Conversation messages")
    model: str = Field(None, description="Model to use (optional, router picks best)")
    max_tokens: int = Field(1024, description="Max tokens in response")
    temperature: float = Field(0.7, description="Sampling temperature (0-2)")
    system_prompt: str = Field(None, description="System prompt")
    provider: str = Field(None, description="Preferred provider (optional)")
    stream: bool = Field(False, description="Stream response token-by-token")


class CompletionResponse(BaseModel):
    content: str
    model: str
    provider: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: float
    cached: bool = False


class TaskRequest(BaseModel):
    agent_id: str = Field(..., description="Agent ID")
    objective: str = Field(..., description="Task objective")
    context: dict = Field(default_factory=dict, description="Task context")
    max_iterations: int = Field(10, description="Max iterations for task")


class TaskResponse(BaseModel):
    task_id: str
    agent_id: str
    objective: str
    status: str
    result: str = None
    error: str = None
    iterations: int
    created_at: str
    started_at: str = None
    completed_at: str = None


class StatsResponse(BaseModel):
    total_requests: int
    strategy: str
    provider_stats: dict
    cache_stats: dict = None
    retry_stats: dict = None
    limiter_stats: dict = None
    orchestrator_stats: dict = None


# ============================================================================
# Global State
# ============================================================================

app_state = {
    "providers": None,
    "router": None,
    "orchestrator": None,
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events"""
    # Startup
    logger.info("Starting CoreAI Protocol Suite...")
    
    providers = load_providers()
    if not providers:
        logger.error("No providers loaded! Check API keys in .env")
        raise RuntimeError("No providers available")
    
    logger.info(f"Loaded providers: {list(providers.keys())}")
    
    app_state["providers"] = providers
    app_state["router"] = Router(
        providers,
        RoutingConfig(strategy=RoutingStrategy.BALANCED),
    )
    app_state["orchestrator"] = Orchestrator()
    
    logger.info("CoreAI server ready")
    
    yield
    
    # Shutdown
    logger.info("Shutting down CoreAI...")


# ============================================================================
# FastAPI App
# ============================================================================

app = FastAPI(
    title="CoreAI Protocol Suite",
    description="Intelligent LLM routing and agent orchestration",
    version="0.1.0",
    lifespan=lifespan,
)


def get_router():
    if not app_state["router"]:
        raise HTTPException(status_code=503, detail="Router not initialized")
    return app_state["router"]


def get_orchestrator():
    if not app_state["orchestrator"]:
        raise HTTPException(status_code=503, detail="Orchestrator not initialized")
    return app_state["orchestrator"]


# ============================================================================
# Endpoints
# ============================================================================


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "providers": list(app_state["providers"].keys()) if app_state["providers"] else [],
    }


@app.post("/v1/completions", response_model=CompletionResponse)
async def completions(
    request: CompletionRequest,
    router: Router = Depends(get_router),
):
    """Generate a completion"""
    try:
        # Convert to provider format
        messages = [
            ProviderMessage(role=m.role, content=m.content)
            for m in request.messages
        ]
        
        provider_request = ProviderCompletionRequest(
            messages=messages,
            model=request.model,
            max_tokens=request.max_tokens,
            temperature=request.temperature,
            system_prompt=request.system_prompt,
        )
        
        # Route and execute
        response = await router.route(provider_request, request.provider)
        
        return CompletionResponse(
            content=response.content,
            model=response.model,
            provider=response.provider,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            cost_usd=response.cost_usd,
            latency_ms=response.latency_ms,
        )
    
    except Exception as e:
        logger.error(f"Completion failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/v1/completions/stream")
async def completions_stream(
    request: CompletionRequest,
    router: Router = Depends(get_router),
):
    """Stream a completion token-by-token"""
    try:
        messages = [
            ProviderMessage(role=m.role, content=m.content)
            for m in request.messages
        ]
        
        provider_request = ProviderCompletionRequest(
            messages=messages,
            model=request.model,
            max_tokens=request.max_tokens,
            temperature=request.temperature,
            system_prompt=request.system_prompt,
            stream=True,
        )
        
        provider = router.providers.get(request.provider or list(router.providers.keys())[0])
        
        async def generate():
            async for chunk in provider.stream(provider_request):
                yield chunk
        
        return StreamingResponse(generate(), media_type="text/event-stream")
    
    except Exception as e:
        logger.error(f"Stream failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/v1/tasks", response_model=TaskResponse)
async def create_task(
    request: TaskRequest,
    orchestrator: Orchestrator = Depends(get_orchestrator),
):
    """Create a new agent task"""
    try:
        task = orchestrator.assign_task(
            request.agent_id,
            request.objective,
            request.context,
        )
        task.max_iterations = request.max_iterations
        orchestrator.task_store.update(task)
        
        return TaskResponse(
            task_id=task.task_id,
            agent_id=task.agent_id,
            objective=task.objective,
            status=task.status.value,
            iterations=task.iterations,
            created_at=task.created_at.isoformat(),
        )
    
    except Exception as e:
        logger.error(f"Task creation failed: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/v1/tasks/{task_id}", response_model=TaskResponse)
async def get_task(
    task_id: str,
    orchestrator: Orchestrator = Depends(get_orchestrator),
):
    """Get task status"""
    task = orchestrator.get_task(task_id)
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


@app.post("/v1/agents/{agent_id}/register")
async def register_agent(
    agent_id: str,
    orchestrator: Orchestrator = Depends(get_orchestrator),
):
    """Register a new agent"""
    orchestrator.register_agent(agent_id)
    return {"agent_id": agent_id, "status": "registered"}


@app.get("/v1/stats", response_model=StatsResponse)
async def get_stats(
    router: Router = Depends(get_router),
    orchestrator: Orchestrator = Depends(get_orchestrator),
):
    """Get system statistics"""
    router_stats = router.stats()
    orch_stats = orchestrator.stats()
    
    return StatsResponse(
        total_requests=router_stats["total_requests"],
        strategy=router_stats["strategy"],
        provider_stats=router_stats["provider_stats"],
        cache_stats=router_stats["cache_stats"],
        retry_stats=router_stats["retry_stats"],
        limiter_stats=router_stats["limiter_stats"],
        orchestrator_stats=orch_stats,
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8743)