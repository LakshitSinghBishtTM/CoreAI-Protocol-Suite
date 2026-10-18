"""
Structured log helpers for completions and agent events.

Request IDs and the per-request access line come from
``coreai.request_context.RequestContextMiddleware``; the logging pipeline is
set up in ``coreai.logging_setup``.
"""

from loguru import logger


def log_completion(
    request_id: str,
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float,
    latency_ms: float,
    cached: bool,
):
    """Structured log line for a completed LLM call."""
    logger.info(
        f"[{request_id}] completion "
        f"provider={provider} model={model} "
        f"in={input_tokens} out={output_tokens} "
        f"cost=${cost_usd:.6f} latency={latency_ms:.0f}ms "
        f"cached={cached}"
    )


def log_agent_event(
    request_id: str,
    agent_id: str,
    event: str,
    detail: str = "",
):
    """Structured log line for agent lifecycle events."""
    logger.info(
        f"[{request_id}] agent={agent_id} event={event}"
        + (f" detail={detail[:120]}" if detail else "")
    )
