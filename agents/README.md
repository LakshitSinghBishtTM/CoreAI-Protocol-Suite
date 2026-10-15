# agents/

**Status: experimental.** This package is not a finished agent framework.

## What exists

- `AgentManager`: spawns, pauses, resumes and terminates agents (capped at 32), runs a periodic health check, restarts unhealthy agents, and broadcasts heartbeats over `protocols/distributed_agent.py`.
- `TaskOrchestrator`: priority queue that dispatches tasks to agents.
- `AutonomousAgent`: holds a context window and calls the model through the kernel.
- `emergency_shutdown.py`: immediate, graceful and forced shutdown modes, exposed at `POST /v1/admin/shutdown`.

## What does not exist yet

- **Tools.** `ToolRouter` returns an empty tool set for every capability. Agents cannot search the web, run code, or read files.
- Checkpointing or resuming a run, step and cost budgets, human approval for risky actions, and run traces.
- Test coverage beyond the lifecycle smoke tests in `tests/test_agent_manager.py`.

## Plan

This package will be rebuilt on top of a tools layer (MCP server and client, permissions, sandboxed execution) rather than extended in place. Until then, treat the API as unstable and do not depend on it.

The experimental "consciousness detection" module that used to be wired into `AgentManager` was removed. It had a memory leak and was never exposed through the API. It remains in git history.
