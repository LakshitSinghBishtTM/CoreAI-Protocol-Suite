from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from loguru import logger

from agents.agent_manager import init_agent_manager
from api import routes
from api.middleware import RateLimitMiddleware
from coreai import Orchestrator, Router, RoutingConfig, RoutingStrategy
from coreai.kernel import init_kernel
from providers import (
    load_providers,
)

# ============================================================================
# Global State
# ============================================================================

app_state = {
    "providers": None,
    "router": None,
    "orchestrator": None,
    "kernel": None,
    "agent_manager": None,
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

    kernel = init_kernel(app_state["router"], app_state["orchestrator"])
    await kernel.start()
    app_state["kernel"] = kernel

    agent_manager = init_agent_manager(kernel, kernel.memory)
    await agent_manager.start_health_monitor()
    app_state["agent_manager"] = agent_manager

    logger.info("CoreAI server ready")

    yield

    # Shutdown
    logger.info("Shutting down CoreAI...")
    if app_state["agent_manager"]:
        await app_state["agent_manager"].shutdown_all()
    if app_state["kernel"]:
        await app_state["kernel"].stop()


# ============================================================================
# FastAPI App
# ============================================================================

app = FastAPI(
    title="CoreAI Protocol Suite",
    description="Intelligent LLM routing and agent orchestration",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(RateLimitMiddleware)
app.include_router(routes.router)


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
        "providers": (
            list(app_state["providers"].keys()) if app_state["providers"] else []
        ),
    }


@app.post("/v1/agents/{agent_id}/register")
async def register_agent(
    agent_id: str,
    orchestrator: Orchestrator = Depends(get_orchestrator),
):
    """Register a new agent"""
    orchestrator.register_agent(agent_id)
    return {"agent_id": agent_id, "status": "registered"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8743)
