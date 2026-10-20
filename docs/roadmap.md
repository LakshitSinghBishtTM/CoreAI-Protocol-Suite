# CaiPS Roadmap: Gateway, Tools, Agents and Local Models

## Summary

CaiPS becomes a self-hostable AI platform for both individuals and teams, built in four layers: a model gateway, a tools provider, an agent harness, and local model support, with each layer usable alone. The plan runs in 11 phases, and the gateway reaches a stable 1.0 before the other layers are built on it.

- **Gateway:** one OpenAI-compatible API in front of cloud and local models, with routing, failover, budgets and caching.
- **Tools provider:** a registry of tools exposed over MCP, with permissions and sandboxing, usable by any model or agent.
- **Agent harness:** a tool-use loop with context management, checkpoints, approvals and full traces.
- **Local models:** run GGUF models like LM Studio does, first by connecting to existing servers, later with a managed llama.cpp runtime.
- **Around them:** observability, cost control, memory, RAG, evals, fine-tuning, guardrails, a web UI, SDKs and plugins.

Credential cleanup (rotating keys, purging git history) is assumed done before Phase 0. Test status of the current code is unverified: the full install timed out and `tests/test_api.py` failed at collection in a minimal setup, so Phase 0 starts there.

## Architecture at a glance

&#91;embedded content: architecture · 4 layers, 2 provider types, cross-cutting services\]

Calls flow down one layer at a time, and only the gateway talks to models, cloud or local. Auth, tracing, budgets and evals apply across all layers.

## Principles and non-goals

Five rules keep a four-layer project from collapsing into one tangled codebase.

1. **Layers depend downward only.** Agents use tools, tools use the gateway, the gateway uses providers. Nothing calls upward.
2. **Every layer ships alone.** Someone can run just the gateway, or gateway plus tools, without the harness.
3. **Claims match tests.** A feature appears in the README only after it has tests and a documented behavior.
4. **Delete before adding.** Each module needs a live reference, an owner and a test, or it moves to `experimental/` or goes.
5. **Measure, don't claim.** Latency, cost and reliability numbers come from a script in the repo.

**Non-goals:** training models from scratch, a hosted SaaS offering, multi-agent swarms, and any "AI consciousness" features. The `consciousness_detection` module is archived.

## Phases 0 and 1: make it green, then cut scope

A fresh clone must install and pass tests, and the repo must shrink to what the four layers need.

**Phase 0: Foundation**

- Reproduce and fix the `tests/test_api.py` collection error; find out why the full install is slow.
- Split dependencies into runtime and dev files; install must finish in minutes.
- Make CI run pytest, ruff and black on every PR, and replace the static "Production" badge with the real CI status.
- Remove tracked junk: `logs/`, `config/*.log`, `dump.sql`, `database/production_backup.db`, `terraform.tfstate`, shell history files. Add `.env.example` with placeholders.
- Delete `scripts/emergency_fix.sh` and `scripts/fix_server.sh`, or fold their fixes into code.
- Move every `TODO.txt` item to a labeled GitHub issue, then delete the file.

**Phase 1: Scope cut**

- Triage each package with one question: does a request need it to be routed, or does a layer in this roadmap need it?
- Keep `providers/`, `coreai/router.py`, limiter, cache, retry, `api/`, `middleware/`, `utils/cost.py` and `utils/tokens.py` as core.
- Archive `neural/consciousness_detection.py`. Keep `neural/training.py` (fine-tuning) as an optional extra, to be hardened in Phase 9.
- Rebuild `agents/` and `protocols/` on top of the tools layer (Phase 7) where the new design needs them, and keep them marked experimental until then.
- Trim `requirements.txt` to what the code imports (about 50 packages now; target under 20). Drop SDKs for providers that aren't supported and duplicates such as two schedulers, two retry libraries and two JWT libraries.
- Consolidate `setup.py`, `setup.cfg` and `pyproject.toml` into one `pyproject.toml`.
- Rewrite the README to describe only what ships.

**Exit criteria:** `pip install -e .[dev] && pytest` passes on a fresh clone, CI is green, and every remaining module has a test and a live reference.

## Phase 2: Gateway reliability

The gateway is the foundation for every other layer, and its failover logic is currently untested (the TODO admits "it works, I just don't know why"). Nothing above it should be built until this phase is done.

**Tests that must exist**

- Failover when the primary returns 5xx, times out, or returns 429 or 529; the router moves to the next provider.
- Circuit breaker: opens after N failures, half-open probe, closes on success, tested with a fake clock.
- Retries: bounded attempts, exponential backoff with jitter, honors `Retry-After`, never retries 4xx or non-idempotent calls.
- Rate limiter: per-key limits under concurrent load.
- All provider tests use recorded or mocked responses. No live API calls in CI.

**Known problems to fix**

- **Anthropic 529 (overloaded):** retry with backoff and jitter, honor `Retry-After`, count toward the breaker, and fail over. Log the status distribution so the cause is visible.
- **DeepSeek reasoner latency spikes and Grok p99 near 30s:** per-provider connect and read timeouts plus a total request deadline. Add optional hedged requests for latency-sensitive routes.
- **OOM kills under load:** profile with `tracemalloc` or `memray`, bound every queue and cache by size and TTL, and add a checked-in load test.
- **Emergency shutdown:** make it a tested kill switch (stop accepting traffic, drain in-flight requests) or delete it.

**New capabilities**

- Declarative routing policies in YAML (cost, latency, reliability weights, allow and deny lists, per-model fallback chains) with schema validation and hot reload.
- A dry-run endpoint that explains why a request would route where it does.
- Per-provider health scoring from recent errors and latency, used by the router.
- Request deadlines and cancellation that propagate to providers.

**Exit criteria:** a chaos script can kill or slow any single provider while the gateway keeps serving, and the behavior is documented.

## Phase 3: Observability, cost and governance

After this phase, anyone can answer "which provider served this request, why, what did it cost and how long did it take" from logs and metrics alone.

- **Request IDs** on every request, carried through logs and to providers that accept them.
- **Structured JSON logs** with one logging library (the repo lists both loguru and structlog), real rotation and size caps. No more 847MB log files.
- **Metrics endpoint** in Prometheus format: requests, errors, latency histograms per provider and model, cache hit rate, breaker state, token counts, cost.
- **Distributed tracing** with OpenTelemetry spans for gateway, provider and (later) tool and agent steps.
- **Cost tracking you can trust:** unit tests for `utils/cost.py` against current price tables, prices in a versioned config file, and a fix for the billing threshold that fires far later than the warning.
- **Budgets and quotas:** soft and hard spend limits per API key, team and model, plus token and request quotas, with clear error responses.
- **Usage reports:** per-key and per-model usage over time, exportable as CSV or JSON.
- **Health endpoints:** `/healthz` (process up) and `/readyz` (dependencies reachable), plus per-provider health in the API.
- **Audit log:** who changed keys, policies or budgets, and when.
- **Routing policy docs:** what the weights mean, with worked examples.

**Exit criteria:** a Grafana dashboard template ships in the repo, and a cost spike or provider outage is visible within one minute.

## Phase 4: API, streaming and packaging

This phase makes CaiPS a drop-in gateway: existing SDKs and tools work by changing a base URL. It ends with the Gateway 1.0 release.

**API surface**

- OpenAI-compatible `/v1/chat/completions`, `/v1/completions`, `/v1/embeddings` and `/v1/models`.
- Streaming over server-sent events, with defined behavior when a provider fails mid-stream (clean error event, no silent truncation).
- Normalized tool calling and structured output (JSON mode, JSON schema) across OpenAI, Anthropic and Gemini formats. *Status: done for all four providers. Streamed tool calls are buffered (complete calls sent as SSE chunks); `json_schema` output is validated with one retry. Local models are open.*
- Vision and image inputs where the provider supports them, with capability flags per model. *Status: done for image URLs and base64 on OpenAI, Grok, Anthropic and Gemini.*
- Optional Anthropic-compatible `/v1/messages` endpoint for clients that expect it. *Status: done (no streamed tool use, no `count_tokens`).*
- Model aliases and virtual models (for example `fast`, `cheap`, `best`) mapped to routing policies.

**Packaging and developer experience**

- `src/` layout, one `pyproject.toml`, single-source versioning, and PyPI releases through trusted publishing.
- Type hints everywhere, `mypy` or `pyright` in CI, `ruff`, and `pre-commit` hooks.
- Slim multi-stage Docker image (non-root, pinned base) with SQLite as the default storage (Postgres optional, migrations tested on both) and a minimal `docker-compose.yml`.
- A five-minute quickstart tested in CI: install, set one key, run, send one `curl`.
- `examples/` for basic routing, fallback chains, budgets and adding a custom provider.
- `CONTRIBUTING.md`, an architecture doc, and a "how to add a provider" guide built on `providers/base.py`.
- Fewer issue and PR templates, placed where GitHub reads them (`.github/ISSUE_TEMPLATE/`).

**Exit criteria:** the OpenAI Python SDK works against CaiPS with only a base URL change, and Gateway 1.0 is tagged and published.

## Phase 5: Local models, connect mode

The fastest route to local GGUF support is a provider for any OpenAI-compatible server. The user runs the model; CaiPS routes to it. This also proves the provider abstraction is clean.

- A generic `openai-compatible` provider with a configurable base URL, optional key and model list.
- Presets for LM Studio, Ollama, `llama-server` (llama.cpp) and vLLM, with auto-discovery of loaded models through `/v1/models`.
- Per-model capability flags (tool calling, vision, JSON mode, context length) so the router and tools layer never assume what a local model can't do.
- Local usage recorded with tokens and latency at zero dollar cost, with an optional hardware or electricity cost estimate.
- Routing rules for local models: keep requests tagged `private` on local only, try local first and fall back to cloud on timeout or queue depth, send small tasks local and hard ones to a frontier model.
- Health checks that mark a local server down quickly, since desktop servers come and go.

**Exit criteria:** a request to a model name configured against LM Studio or Ollama works through the same API as a cloud model, with failover to a cloud model when the local server is down.

## Phase 6: Tools provider

CaiPS becomes the place tools live: registered once, permissioned, and usable by any model, client or agent. It builds on MCP (Model Context Protocol) instead of inventing a format.

**Core**

- **Tool registry:** each tool has a name, JSON schema, description, version, owner, timeout and permission level (read-only, write, destructive).
- **MCP server:** expose the registry so any MCP client (editors, other agents, desktop apps) can use CaiPS tools.
- **MCP client:** connect to external MCP servers over stdio and HTTP and add their tools to the registry, with per-server allow lists.
- **Tool-call execution in the gateway:** when a model returns a tool call, CaiPS can run it and continue the conversation, or hand it back to the client. Selectable per request.
- **Permissions and policy:** per-key and per-agent tool allow lists, argument validation against the schema, and rate limits per tool.
- **Approvals:** destructive tools pause for human approval through an API and webhook.

**Built-in tools**

- HTTP fetch with domain allow lists, file read and write inside a scoped workspace, web search through a configurable provider, and a calculator.
- Sandboxed code execution (container or microVM) with no network by default and CPU, memory and time limits.
- Database query tool with read-only mode, and a shell tool disabled by default.

**Quality and safety**

- Treat tool output as untrusted input: label it, cap its size, and strip or flag instruction-like content to limit prompt injection.
- Record every tool call (arguments, result, duration, caller) in the audit log and traces.
- Tool test harness: replay recorded calls against a tool to catch regressions.
- Capability negotiation so tools aren't offered to local models that can't call them reliably.

**Exit criteria:** an external MCP client can list and call CaiPS tools, and a model request can use a registered tool end to end with permissions enforced and the call visible in traces.

## Phase 7: Agent harness

Rebuild `agents/` as a small, inspectable harness on top of the gateway and tools layers. Start with one pattern that works (a tool-use loop) before adding anything multi-agent.

**The loop**

- Plan, call the model, run tools, observe, repeat, until done or a limit is hit.
- Hard limits per run: max steps, token and cost budget, and wall-clock deadline. Budgets are enforced by the gateway, not just the agent.
- Agent definitions as config: system prompt, model or routing policy, allowed tools, limits, and memory settings.
- Run API: start, status, cancel, pause, resume, and stream events.

**Context and state**

- Context management: token counting, truncation and summarization when the window fills, with pinned instructions that never drop.
- Checkpointing after every step so a crashed or paused run resumes where it stopped.
- Durable run storage (database) with the full message and tool history.

**Control**

- Human-in-the-loop approvals for destructive tools and for steps the agent flags as uncertain.
- Loop and stall detection (same tool call repeated, no progress) that stops the run with a clear reason.
- Tighter default limits and shorter plans when the backing model is a small local model.
- Kill switch: stop all runs for a key, team or the whole instance. This replaces `emergency_shutdown.py`.

**Visibility**

- A full trace per run (every model call, tool call, token count, cost) that can be replayed and diffed.
- Run viewer in the API first, UI later (Phase 10).

**Later patterns, only after the loop is solid**

- Sub-agents that run as tool calls with their own budgets.
- Scheduled and event-triggered runs (cron, webhooks).
- Reusable workflows with fixed steps for tasks that shouldn't be left to a model's judgment.

**Exit criteria:** a defined agent completes a multi-step task using tools, survives a mid-run restart through a checkpoint, respects its budget, and its trace shows every step.

## Phase 8: Local models, managed GGUF runtime

With this phase, dropping a `.gguf` file into a folder is enough, as in LM Studio. CaiPS launches and supervises `llama-server` (llama.cpp) as a child process and talks to it over localhost HTTP. It does not embed inference in the gateway process, so a native crash or out-of-memory kill can't take down routing, auth or the other providers.

**Model registry**

- A local models directory scanned for `.gguf` files, with metadata read from each header: architecture, parameter count, quantization, trained context length and embedded chat template.
- Capability flags per model (tools, vision, JSON mode, embeddings), detected where possible and overridable in config.
- License and source shown per model, so users see terms before use.

**Downloads**

- Pull from Hugging Face by name and quantization (for example `org/repo:Q4_K_M`), with resumable downloads, disk-space checks and SHA-256 verification.
- Delete, list and inspect models through the API and CLI.

**Runtime management**

- Detect an installed `llama-server`, or download an official prebuilt release for the platform. Pin a tested version and update on a schedule. CaiPS does not compile llama.cpp for users.
- Hardware detection (CUDA, Metal, Vulkan, CPU) and automatic choice of GPU layer offload and context size to fit available memory, with manual overrides.
- Load on first request, unload after an idle timeout, and evict the least recently used model when memory runs short. One child process per loaded model, bound to localhost only.
- Parallel request slots with a gateway-side queue and a clear "busy" response instead of piling requests on the server.
- Supervision: health checks, restart with backoff, and crash logs captured per model.

**API parity**

- Same OpenAI-compatible endpoints as cloud models: chat, completions, streaming and embeddings.
- Chat templates taken from the GGUF itself through llama.cpp rather than hand-written per model.
- Optional speculative decoding with a draft model and prompt-cache reuse as later optimizations.

**Exit criteria:** a new user drops a GGUF into the models folder, sends one request, and gets a streamed response with no other setup. A killed model process restarts without affecting cloud routing.

Ship this as an optional extra (`caips[local]`) so the base gateway stays light.

## Phase 9: Memory, RAG, evals, fine-tuning and guardrails

These features only pay off once real agent runs exist to measure and protect. Evals come first, because without them no later change can be judged.

**Evals (first)**

- A small suite that replays tasks against agents, models and routing configs and scores the results (exact match, rubric, or model-graded).
- Regression runs in CI on a schedule, with cost and latency recorded next to quality.
- Routing evals that compare policies, for example local-first against cloud-only, on the same tasks.
- User feedback (thumbs up or down, corrections) stored against requests, usable as an eval source.

**Memory**

- Short-term: run state and conversation history, already covered by checkpointing in Phase 7.
- Long-term: per-user and per-agent memory in a key-value or vector store, with explicit write and forget operations and a way to view what is stored.
- Retention limits and deletion on request.

**RAG**

- An embeddings endpoint (cloud and local) and a vector store abstraction (SQLite-vec or pgvector first).
- Document ingestion: text, Markdown, PDF and HTML with chunking, metadata and re-indexing.
- A retrieval tool in the tools layer, so any agent can search a collection with citations.

**Guardrails**

- Input and output filters: PII detection and redaction, secret detection, and configurable blocked topics.
- Prompt-injection defenses for tool results and retrieved documents.
- Output validation against JSON schemas with automatic retry.
- Policy per key or team: which models, tools and data an agent may use.

**Caching**

- Exact-match response cache first, then optional semantic cache, both opt-in with clear privacy notes and per-tenant isolation.

**Fine-tuning**

- Keep the existing OpenAI fine-tuning endpoints (`neural/training.py`) as an optional extra, with tests against recorded responses.
- Dataset upload with format and size validation before any paid job starts.
- Job tracking: status, logs, a cost estimate before launch, actual cost after, and cancel.
- Finished models register in the router automatically, with aliases and budgets like any other model.
- Later: fine-tuning APIs from other providers, and LoRA adapters for local GGUF models where llama.cpp tooling supports it.

**Exit criteria:** an eval suite runs in CI, an agent can use a document collection with citations, a fine-tuned model can be launched, tracked and routed like any other, and PII redaction is on by default for logs.

## Phase 10: Platform and ecosystem

The last phase turns the layers into something both self-hosters and teams can adopt without reading the source.

**Web UI and CLI**

- Admin UI: providers and health, keys, budgets, routing policies, models (local and cloud), and usage.
- Agent UI: define agents, start runs, watch traces live, approve tool calls, and replay past runs.
- Playground for comparing models side by side on the same prompt.
- Model manager for local GGUF files: download, load, unload and see memory use.
- `caips` CLI for the same operations, so everything is scriptable.

**Multi-tenancy and access**

- Organizations, teams and users with roles (admin, developer, viewer), and per-tenant data isolation.
- SSO through OIDC and SAML, scoped API keys, key rotation and expiry.
- Per-tenant budgets, rate limits, tool allow lists and log retention. Single-user mode stays the default, so self-hosters never need tenants or SSO.

**SDKs and integrations**

- Official Python and TypeScript SDKs (thin, since the API is OpenAI-compatible) with typed agent and tool clients.
- Plugin system for providers, tools, guardrails and routing strategies, with a stable plugin API and a registry of community plugins.
- Webhooks and event streams for run completion, budget alerts and provider outages.
- Notifications to Slack and email for alerts.

**Deployment**

- Helm chart and Kubernetes manifests, a Terraform example, and a single-binary or single-container local mode.
- SQLite by default for single-node and self-hosted use, with optional Postgres and Redis for horizontal scaling, and zero-downtime config reload.
- Backup and restore for the database and model registry, plus a documented upgrade path between versions.
- Optional OpenAPI-driven docs site and a public status page template.

**Exit criteria:** a new team can deploy CaiPS with the Helm chart, create a tenant, add providers and a local model, run an agent with approvals, and see its cost, all through the UI.

## Feature catalog

Every feature in this roadmap, sorted by phase. Must means the layer doesn't work without it, Should means expected by most users, Could means worth building if time allows.

| Feature | Layer | Phase | Priority |
| --- | --- | --- | --- |
| Green CI, split dependencies, repo cleanup | Foundation | 0 | Must |
| Scope cut, archive experimental modules | Foundation | 1 | Must |
| Failover, circuit breaker, retries with jitter | Gateway | 2 | Must |
| Per-provider timeouts and request deadlines | Gateway | 2 | Must |
| Declarative routing policies and dry-run explain | Gateway | 2 | Must |
| Hedged requests for latency-sensitive routes | Gateway | 2 | Could |
| Request IDs, JSON logs, Prometheus metrics | Gateway | 3 | Must |
| OpenTelemetry tracing | Gateway | 3 | Should |
| Verified cost tracking, price tables | Gateway | 3 | Must |
| Budgets and quotas per key and team | Gateway | 3 | Must |
| Audit log | Gateway | 3 | Should |
| OpenAI-compatible chat, embeddings, models API | Gateway | 4 | Must |
| Streaming with mid-stream failure handling | Gateway | 4 | Must |
| Normalized tool calling and structured output | Gateway | 4 | Must |
| Model aliases and virtual models | Gateway | 4 | Should |
| Anthropic-compatible `/v1/messages` | Gateway | 4 | Could |
| PyPI release, slim Docker image, quickstart | Foundation | 4 | Must |
| SQLite by default, optional Postgres, migrations tested on both | Foundation | 4 | Must |
| OpenAI-compatible provider (LM Studio, Ollama, llama-server, vLLM) | Local models | 5 | Must |
| Model capability flags | Local models | 5 | Must |
| Local-first and privacy routing rules | Local models | 5 | Should |
| Tool registry with schemas and permissions | Tools | 6 | Must |
| MCP server and MCP client | Tools | 6 | Must |
| Tool execution in the gateway | Tools | 6 | Should |
| Built-in tools (HTTP, files, search, calculator) | Tools | 6 | Should |
| Sandboxed code execution | Tools | 6 | Should |
| Approval workflow for destructive tools | Tools | 6 | Must |
| Tool-use agent loop with budgets and limits | Agents | 7 | Must |
| Context management and summarization | Agents | 7 | Must |
| Checkpointing and resume | Agents | 7 | Must |
| Run traces with replay | Agents | 7 | Must |
| Agent kill switch | Agents | 7 | Must |
| Sub-agents, scheduled and event-triggered runs | Agents | 7 | Could |
| GGUF model registry with metadata | Local models | 8 | Must |
| Managed `llama-server` lifecycle (load, idle unload, LRU evict) | Local models | 8 | Must |
| Hugging Face downloads with checksums | Local models | 8 | Should |
| Hardware detection and offload tuning | Local models | 8 | Should |
| Speculative decoding, prompt-cache reuse | Local models | 8 | Could |
| Eval suite and CI regression runs | Quality | 9 | Must |
| Long-term memory with forget controls | Agents | 9 | Should |
| Embeddings, vector store, document ingestion, retrieval tool | RAG | 9 | Should |
| PII redaction and prompt-injection defenses | Guardrails | 9 | Must |
| Exact-match cache, then semantic cache | Gateway | 9 | Could |
| Fine-tuning endpoints and job tracking (optional extra) | Models | 9 | Should |
| Admin UI, agent UI, playground, model manager | Platform | 10 | Should |
| `caips` CLI | Platform | 10 | Should |
| Multi-tenancy, roles, SSO | Platform | 10 | Should |
| Python and TypeScript SDKs | Platform | 10 | Should |
| Plugin system and registry | Platform | 10 | Could |
| Webhooks, event streams, Slack and email alerts | Platform | 10 | Could |
| Helm chart, scaling, backup and restore | Platform | 10 | Should |

## Security model across layers

A gateway that holds provider keys, runs tools and executes agent loops is a high-value target, so security is designed in from Phase 2, not added at the end.

- **Secrets:** read from the environment or a secrets manager, never from tracked files. Add secret scanning (gitleaks) and a pre-commit hook in CI.
- **Authentication:** scoped API keys with expiry and rotation, JWT for UI sessions, signed webhooks, and SSO later.
- **Authorization:** every request is checked against the key's allowed models, tools and budgets. Deny by default for tools.
- **Tools and agents:** sandbox code execution (no network by default, resource limits), scoped file workspaces, domain allow lists for HTTP, approvals for destructive actions.
- **Prompt injection:** treat tool output, web pages and retrieved documents as untrusted data. Label them, cap their size, and never let them change permissions or tool allow lists.
- **Local runtime:** bind model servers to localhost only, verify model file hashes, keep llama.cpp updated (it parses untrusted file formats), and treat third-party GGUF files like any other download.
- **Data handling:** PII redaction in logs by default, configurable log retention, per-tenant isolation, and no prompt or response storage unless enabled.
- **Supply chain:** pin and hash dependencies, run `pip-audit` in CI, wait out brand-new releases before upgrading, and publish releases with provenance.
- **Vulnerability handling:** a `SECURITY.md` with a reporting address and response targets, and a threat model document reviewed at each major phase.

## Testing, release and documentation standards

These apply to every phase, so the project stays shippable as it grows.

**Testing**

- Unit tests for routing, retry, breaker, limiter, cost and permission logic. Target 85% coverage on those modules.
- Contract tests per provider using recorded responses, plus a scheduled job against live APIs with tiny requests to catch API drift.
- Chaos tests that kill or slow providers, local model processes and tool servers.
- Load tests with a checked-in script and published p50 and p99 numbers for gateway overhead.
- Security tests: permission bypass attempts, prompt-injection fixtures, and sandbox escape checks for the code tool.

**Release**

- Semantic versioning, a `CHANGELOG.md`, and tagged releases built in CI.
- Alpha, beta and stable labels per layer, so the README can say "gateway: stable, agents: alpha" honestly.
- Database migrations tested forward and backward, with a documented upgrade path.
- Deprecation policy: one minor version of warnings before removal.

**Documentation**

- README: what it is, install, quickstart, supported providers, honest status per layer. No manifesto.
- `docs/architecture.md` with the request lifecycle (auth, limits, cache, routing, provider, retry and failover, response).
- `docs/routing.md` for policy weights and debugging a decision.
- `docs/operations.md`: the missing incident runbook (provider outage, latency, cost spike, memory growth, model process crash).
- Per-layer guides for tools, agents and local models, each with a runnable example.

## Success metrics

Each target is measured by a script or dashboard in the repo, not asserted in the README.

| Metric | Target | Measured by |
| --- | --- | --- |
| Fresh clone to green tests | Under 5 minutes, one command | CI job on a clean runner |
| Runtime dependencies | Under 20 | Dependency report in CI |
| Coverage on router, retry, breaker, limiter | 85% or more | Coverage report |
| Requests failed when one provider is down | Zero in chaos test | Chaos script |
| Gateway overhead added to a request | p99 published, target under 20 ms | Load test script |
| Time to first request for a new user | Under 5 minutes | Tested quickstart |
| OpenAI SDK compatibility | Works with base URL change only | Compatibility test suite |
| Cost accuracy | Within 1% of provider invoices | Monthly reconciliation |
| Local model first-token time after cold load | Published per model size | Benchmark script |
| Agent run success on the eval suite | Tracked per release, no regressions | Eval suite in CI |
| Agent runs that exceed their budget | Zero | Budget enforcement tests |
| README claims without a test or benchmark | Zero | Docs review checklist |

## Decisions

Five open questions are settled, and the phases above reflect them.

| Decision | Choice | What it means for the plan |
| --- | --- | --- |
| License | Stays GPL-3.0, no change | No relicensing work; nothing in the plan depends on it. |
| Agent and protocol code | Rebuild `agents/` and `protocols/` where the new design needs them | Phase 7 reuses what fits and replaces the rest. Nothing stays half-wired. |
| Fine-tuning | Keep it | Stays an optional extra and is hardened in Phase 9. |
| Storage | SQLite by default | Postgres is optional for multi-node setups; migrations are tested on both. |
| Audience | Both self-hosters and teams | Single-user mode is the default; tenants, roles and SSO are opt-in (Phase 10). |

## First 15 issues to open

These cover Phases 0 to 2 and unblock everything after them.

- [ ] Fix the test collection error in `tests/test_api.py` and document the install problem
- [ ] Split runtime and dev dependencies; remove unused packages
- [ ] Get CI green (pytest, ruff, black) and show a real status badge
- [ ] Remove tracked logs, dumps, state files and history files; add `.env.example`
- [ ] Consolidate packaging into one `pyproject.toml`
- [ ] Move `TODO.txt` items into labeled GitHub issues
- [ ] Write the scope decision: keep, archive or remove each package (`agents/`, `protocols/`, `neural/`)
- [ ] Archive `consciousness_detection`; keep fine-tuning as an optional extra
- [ ] Rewrite the README to match what ships
- [ ] Write failover and circuit breaker tests
- [ ] Handle Anthropic 529 with backoff, jitter, `Retry-After` and failover
- [ ] Add per-provider timeouts and a request deadline
- [ ] Profile memory under load and bound all queues and caches
- [ ] Define the YAML routing policy schema with a dry-run endpoint
- [ ] Add `SECURITY.md`
