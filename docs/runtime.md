# Runtime

There is no separate runtime layer in CoreAI Protocol Suite.

This document previously described a `runtime/` package in detail - a Runtime Engine with its own lifecycle (COLD → INITIALIZING → READY → DEGRADED → SHUTTING DOWN → STOPPED), a Distributed Runtime coordinator, worker registration and heartbeats, load balancing, and optional GPU acceleration. That package had zero callers and zero test coverage anywhere in the codebase and was removed as dead code rather than left half-built. See "Distributed Execution" under Architectural Goals in [architecture.md](architecture.md) for what's actually planned versus built today.

What actually handles execution:

- **Completion requests** (`/v1/completions`) are routed directly by `coreai/router.py` to one of the five provider adapters in `providers/`, which call the hosted provider APIs (OpenAI, Anthropic, Gemini, Grok, DeepSeek). There is no local model inference and nothing resembling the pipeline this file used to describe sitting between the router and those providers.
- **Agent tasks** are coordinated by `agents/agent_manager.py` and delivered through the Distributed Agent Protocol, sealed and verified via the Secure Transport Protocol before delivery. See [protocols.md](protocols.md).

If CoreAI grows a real multi-node execution layer in the future, this is where it should be documented again - against the actual implementation, not ahead of it.
