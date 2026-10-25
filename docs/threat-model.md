# Threat model

Review this file at each major phase. Last reviewed: 0.1.0 (alpha).

## Assets
- Provider API keys (OpenAI, Anthropic, Gemini, Grok, DeepSeek) and CaiPS tokens/API keys.
- Request and response content, agent memory, RAG documents, run checkpoints.
- Budgets and cost data; the audit log.
- The host running tools, the code sandbox and local model processes.

## Trust boundaries
1. Client to gateway (untrusted network, authenticated by JWT or `X-API-Key`).
2. Gateway to cloud providers (TLS, our keys leave the process only here).
3. Agent harness to tools (model output is untrusted input).
4. Gateway to local runtime (`llama-server` child process, GGUF files).
5. Gateway to MCP servers and webhooks (third-party code and endpoints).

## Threats and current mitigations
| Threat | Mitigation today | Gap |
| --- | --- | --- |
| Stolen or guessed token | Scopes, per-key budgets, rate limits | No rotation or expiry (roadmap 8); no token-issuing endpoint |
| Provider key leakage via logs or errors | Log masking of bearer tokens and keys | No automated test over every provider error path |
| Prompt injection driving tool use | Optional guardrails, human approvals, sandbox | Sandbox is process level, not container or microVM |
| Malicious or oversized tool output | Output limits in the tools layer | Not fuzzed |
| Malicious GGUF file | None beyond user choice of source | Treat as untrusted code input; pin `llama.cpp` releases |
| Runaway cost | Budgets enforced per key and per run | Runtime budget edits reset on restart |
| Cross-tenant data access | Single-user mode only | Multi-tenancy not started (roadmap 8) |
| Webhook forgery or SSRF | HMAC signing | Verify outbound URL allow-listing |
| Replica inconsistency | None | Circuit breaker state is per process |
| Custom crypto flaws | Not on the request path | `protocols/` is unaudited; see below |
| Secrets in git history | Gitleaks in CI on current tree | Past commits contain secret files; see `docs/secret-history-audit.md` |

## Out of scope
Training-data poisoning, model weights theft, and physical access to the host.

## Open decisions
- `protocols/`: `agents/agent_manager.py` imports `protocols.distributed_agent`, which uses the unaudited `secure_protocol.py`. Either rebuild on the tools layer or archive the package and remove the import. This needs an owner decision; it was not changed here.
