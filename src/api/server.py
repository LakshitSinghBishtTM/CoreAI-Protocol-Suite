from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse
from loguru import logger

from agents.agent_manager import init_agent_manager
from api import anthropic_compat, openai_compat, routes
from api.auth import AuthContext, require_scope
from api.middleware import RateLimitMiddleware
from coreai import Orchestrator, Router, RoutingConfig, RoutingStrategy, tracing
from coreai.drain import DrainController, DrainMiddleware
from coreai.kernel import init_kernel
from coreai.logging_setup import configure_logging
from coreai.metrics import CONTENT_TYPE, render_prometheus
from coreai.request_context import RequestContextMiddleware
from coreai.task_runner import TaskRunner
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
    "task_runner": None,
    "budgets": None,
    "audit": None,
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events"""
    # Startup
    configure_logging()
    tracing.setup_tracing()  # no-op unless enabled; see coreai/tracing.py
    logger.info("Starting CoreAI Protocol Suite...")

    # Database-backed API-key auth needs the engine initialised.
    from database import db as _db

    _db.init_db()
    app_state["db"] = True

    providers = load_providers()
    if not providers:
        logger.error("No providers loaded! Check API keys in .env")
        raise RuntimeError("No providers available")

    logger.info(f"Loaded providers: {list(providers.keys())}")
    for p in providers.values():
        if getattr(p, "is_local", False):
            await p.refresh_models()  # never raises; a dead server is skipped

    app_state["providers"] = providers
    app_state["router"] = Router(
        providers,
        RoutingConfig.from_env(
            strategy=RoutingStrategy.BALANCED,
            rate_limits=_load_rate_limits(),
        ),
        policies=_load_policy_store(providers),
    )
    app_state["orchestrator"] = Orchestrator()
    app_state["budgets"] = _load_budgets()

    kernel = init_kernel(app_state["router"], app_state["orchestrator"])
    await kernel.start()
    app_state["kernel"] = kernel

    agent_manager = init_agent_manager(kernel, kernel.memory)
    await agent_manager.start_health_monitor()
    app_state["agent_manager"] = agent_manager
    app_state["task_runner"] = TaskRunner(app_state["orchestrator"], agent_manager)

    logger.info("CoreAI server ready")

    yield

    # Shutdown
    logger.info("Shutting down CoreAI...")
    if app_state.get("task_runner"):
        await app_state["task_runner"].shutdown()
    if app_state["agent_manager"]:
        await app_state["agent_manager"].shutdown_all()
    if app_state["kernel"]:
        await app_state["kernel"].stop()
    if app_state.get("db"):
        await _db.close_db()
        app_state["db"] = False
    tracing.shutdown_tracing()  # flush queued spans


# ============================================================================
# FastAPI App
# ============================================================================

drain = DrainController()

app = FastAPI(
    title="CoreAI Protocol Suite",
    description="Intelligent LLM routing and agent orchestration",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(RateLimitMiddleware)
app.add_middleware(DrainMiddleware, controller=drain)
# Added last so it is outermost: every response, even a drain 503, gets an ID.
app.add_middleware(RequestContextMiddleware)
app.include_router(routes.router)
app.include_router(openai_compat.router)
app.include_router(anthropic_compat.router)


def _load_rate_limits():
    """Per-provider limits from providers.<name>.rate_limits in the routing
    file. Without a valid file, providers keep the built-in defaults."""
    import os

    from coreai.policy import PolicyError, load_rate_limits

    path = os.environ.get("COREAI_ROUTING_POLICY_FILE", "config/routing.yml")
    if not os.path.exists(path):
        return {}
    try:
        return load_rate_limits(path)
    except PolicyError as exc:
        logger.error(f"Rate limits from {path} ignored: {exc}")
        return {}


def _load_budgets():
    """Per-key budgets from COREAI_BUDGET_FILE (default config/budgets.yml).

    No file means no limits. An invalid file is an error at startup rather
    than silently running without the limits the operator asked for.
    """
    import os

    from coreai.budgets import load_budgets

    path = os.environ.get("COREAI_BUDGET_FILE", "config/budgets.yml")
    if not os.path.exists(path):
        return None
    manager = load_budgets(path)  # raises BudgetConfigError with all problems
    logger.info(f"Budgets active from {path}")
    get_audit().record(
        "budget.config_loaded",
        actor="system",
        target=path,
        details={
            "default": manager.default is not None,
            "subjects": len(manager.subjects),
        },
    )
    return manager


def _load_policy_store(providers):
    """Declarative routing policies, if a policy file exists.

    COREAI_ROUTING_POLICY_FILE overrides the default config/routing.yml.
    An invalid file means the gateway starts without policies and logs why.
    Providers named in a policy that aren't loaded are simply never used.
    """
    import os

    from coreai.policy import PolicyStore

    path = os.environ.get("COREAI_ROUTING_POLICY_FILE", "config/routing.yml")
    if not os.path.exists(path):
        return None
    store = PolicyStore(path, known_providers=None)
    audit = get_audit()
    audit.record(
        "policy.loaded" if not store.last_error else "policy.load_failed",
        actor="system",
        target=path,
        outcome="ok" if not store.last_error else "rejected",
        details={"problems": store.last_error} if store.last_error else {},
    )
    if store.last_error:
        logger.error(f"Routing policies not active: {store.last_error}")
        return None
    store.on_event = lambda outcome, details: audit.record(
        "policy.reload", actor="system", target=path, outcome=outcome, details=details
    )
    return store


def get_router():
    if not app_state["router"]:
        raise HTTPException(status_code=503, detail="Router not initialized")
    return app_state["router"]


def get_audit():
    """The audit log (in memory; also a JSONL file if COREAI_AUDIT_FILE is set)."""
    if app_state.get("audit") is None:
        import os

        from coreai.audit import AuditLog

        app_state["audit"] = AuditLog(path=os.environ.get("COREAI_AUDIT_FILE"))
    return app_state["audit"]


def get_usage_ledger():
    """The usage ledger (always on; bounded in memory)."""
    if app_state.get("usage") is None:
        from coreai.usage import UsageLedger

        app_state["usage"] = UsageLedger()
    return app_state["usage"]


def get_budgets():
    """The budget manager, or None when no limits are configured."""
    return app_state.get("budgets")


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


@app.get("/healthz")
async def liveness():
    """Liveness: the process is up and serving. No dependency checks."""
    return {"status": "ok"}


@app.get("/readyz")
async def readiness():
    """Readiness: accepting traffic, router initialized, and at least one
    provider whose circuit breaker would admit a call."""
    router = app_state["router"]
    reasons = []
    if not drain.accepting:
        reasons.append("draining")
    if router is None:
        reasons.append("router not initialized")
    else:
        usable = [n for n, b in router.breakers.items() if b.would_allow()]
        if not usable:
            reasons.append("no provider available (all circuits open)")
    body = {"status": "ready" if not reasons else "not_ready", "reasons": reasons}
    return JSONResponse(body, status_code=200 if not reasons else 503)


@app.get("/metrics", response_class=PlainTextResponse)
async def metrics(ctx: AuthContext = Depends(require_scope("admin"))):
    """Prometheus metrics. Needs an admin-scope bearer token."""
    router = get_router()
    text = render_prometheus(
        router.stats(), accepting=drain.accepting, in_flight=drain.in_flight
    )
    return PlainTextResponse(text, media_type=CONTENT_TYPE)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8743)
