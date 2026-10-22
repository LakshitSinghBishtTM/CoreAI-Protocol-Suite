# Runtime

There is no separate runtime layer in CoreAI Protocol Suite.

This document previously described a `runtime/` package in detail - a Runtime Engine with its own lifecycle (COLD → INITIALIZING → READY → DEGRADED → SHUTTING DOWN → STOPPED), a Distributed Runtime coordinator, worker registration and heartbeats, load balancing, and optional GPU acceleration. That package had zero callers and zero test coverage anywhere in the codebase and was removed as dead code rather than left half-built. See "Distributed Execution" under Architectural Goals in [architecture.md](architecture.md) for what's actually planned versus built today.

What actually handles execution:

- **Completion requests** (`/v1/completions`) are routed directly by `coreai/router.py` to one of the five provider adapters in `providers/`, which call the hosted provider APIs (OpenAI, Anthropic, Gemini, Grok, DeepSeek). Local models are reached through the same router: `providers/local.py` talks to OpenAI-compatible servers (LM Studio, Ollama, `llama-server`, vLLM), and `localrt/` manages GGUF models and a llama.cpp supervisor. See [local-models.md](local-models.md). Nothing resembling the pipeline this file used to describe sits between the router and the providers.
- **Agent runs** (`/v1/runs`) are executed by the harness in `harness/`: a tool-use loop with limits, checkpoints and approvals. See [agents.md](agents.md).
- **Legacy agent tasks** are coordinated by `agents/agent_manager.py` and delivered through the Distributed Agent Protocol, sealed and verified via the Secure Transport Protocol before delivery. See [protocols.md](protocols.md).

If CoreAI grows a real multi-node execution layer in the future, this is where it should be documented again - against the actual implementation, not ahead of it.

## Routing policies

`config/routing.yml` (override with `COREAI_ROUTING_POLICY_FILE`) defines named policies. A request picks one with `metadata["policy"]`; otherwise the policy named `default` is used.

| Key | Meaning |
| --- | --- |
| `strategy` | `cost_aware`/`balanced`, `cheapest`, `latency_first`, `round_robin`, `fallback`, `explicit` |
| `weights` | `cost` and `latency`, normalised to sum to 1 (balanced only) |
| `latency_ceiling_ms` | providers whose recent p50 is above this are tried last, not dropped |
| `allow` / `deny` | providers a policy may or may not use; failover never leaves the allowed set |
| `fallback_chain` | failover order after the primary; for `fallback` it is the whole order |
| `provider` | the pinned provider for `explicit` |

Open circuit breakers always exclude a provider. The file is re-checked every couple of seconds; a file that fails validation is logged and ignored, and the previous policies stay active.

To see what a request would do without sending it, call `POST /v1/routing/explain` with the same body fields as a completion plus an optional `policy`.
