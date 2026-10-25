# Changelog

## 0.1.0 (unreleased)

The previous `1.0.0` label did not match the project's state; versions now start at 0.1.0 (alpha).

### Changed (storage and packaging)
- Migrations run at startup (`COREAI_AUTO_MIGRATE=0` disables); on Postgres an advisory lock lets replicas start together.
- Fixed Postgres migrations, which could not apply through asyncpg (`applied_at` was sent as a string). The Postgres migration files no longer need the `uuid-ossp` or `pg_stat_statements` extensions (`gen_random_uuid()` is built in from PG 13).
- Backups cover Postgres: `--pg DSN` (or `DATABASE_URL`) runs `pg_dump`, and `restore --pg-restore-to DSN` runs `pg_restore`. Passwords stay out of argv and the manifest.
- `mypy` now checks all of `src` in CI (the ratchet list is gone). Only the Anthropic and Gemini adapter modules have four error codes relaxed, because the SDK types change between releases.
- CI runs `twine check --strict` on the built wheel and sdist.

### Added (SDKs and tracing)
- Typed clients: `caips_sdk.types` (`TypedDict` shapes) and typed return values in the Python client, exported interfaces in the TypeScript client, plus `list_agents`, `list_runs`, `run_events`, `pause_run`, `resume_run`, `kill_runs`, `run_agent` and `wait_run(stop_when_blocked=True)` in both. Tests check the Python types against the store schema. CI now builds and tests the TypeScript SDK.
- Tracing for agent runs: `caips.agent.run`, `caips.agent.model_call` and `caips.agent.plan` spans, with the existing `caips.tool` spans nested under the run. No task text, replies or tool arguments reach spans.
- Usage report export (JSON and CSV, by subject and model) is confirmed by the existing tests in `tests/test_usage.py`; the item is closed.

### Added (evals)
- Agent tasks in the eval suite: `agent: <name>` runs the agent through `/v1/runs` and scores its result, recording run id, steps, cost and tokens.
- `--grader-model` on `python -m evals` and `caips eval` grades `rubric` tasks through the gateway; without it, rubric tasks report that a grader is needed.

### Added (agent planning step)
- `plan: true` on an agent (with `plan_max_steps`) adds one tool-free planning call before the first step. The plan is stored in the run's first message, so it is checkpointed, survives compaction and is not repeated on resume; its cost and tokens count toward the agent's limits. Off by default. Removed the finished agent harness and Postgres-tool items from the roadmap (the Postgres run store and `db_query` support were already done).

### Changed (release and documentation)
- Incident runbook in `docs/operations.md` now covers provider outage, high latency, cost spike, memory growth and local model crash; added `docs/deprecation-policy.md`; README status labels are defined and the stale contract-test claim is fixed. Removed the finished security and release sections from the roadmap.

### Changed (security)
- Removed the unwired `middleware/` package (the server uses `api/middleware.py`); its tests now import `RateLimitMiddleware` from `api.middleware`, and the validator tests were dropped with the code.
- Added `docs/threat-model.md` and `docs/secret-history-audit.md` (secret files found in git history; rotation and purge are owner actions).

### Added (provider contract tests)
- Contract tests for Gemini, Grok and DeepSeek: the real provider classes and SDKs over mock HTTP transports, covering complete, streaming, request wire format, base URLs, error classification and failover (`tests/test_provider_contracts.py`).

### Added (Anthropic-compatible count_tokens)
- `POST /v1/messages/count_tokens` returns `{"input_tokens": n}`, estimated with a provider's local tokenizer over the system prompt, messages, tool calls and tool schemas (images not counted). Same validation as `/v1/messages`; scope `completions:read`; header `X-CaiPS-Estimate: true`.

### Added (packaging checks)
- `tests/test_packaging_claims.py` enforces: runtime dependencies under 20 (12 today), loguru as the only logging library with stdlib logging bridged into it, and gitleaks and pip-audit in CI.

### Added (MCP client reconnect)
- The MCP client reconnects (close, restart, re-handshake) after a dropped stdio or HTTP connection. A request that never reached the server, and `tools/list`, are retried once; a `tools/call` lost mid-flight is not retried, so a tool never runs twice.

### Added (TUI: easier start, separate settings)
- **Home** section: first-run checklist (gateway, token, provider key) with an inline token field. The TUI opens on it until a token is saved, then straight on Chat.
- **Settings** section: chat defaults, theme and sign-out in one place, saved to `tui.json` in the config directory. The chat screen's session panel is collapsed by default.
- **Harness** is renamed **Chat** and renders replies as Markdown; the raw **API** pane is now **API playground** under Advanced.
- `caips tui` / `caips-tui` no longer force `--section harness`.

### Changed (TUI redesign)
- `caips-tui` has a grouped sidebar (Workspace, Gateway, Automation, Data, Advanced), a slim top bar with a breadcrumb, and opens on the new **Harness** section.
- **Harness**: a chat session with model, provider, policy, temperature, max tokens, history window, system prompt, context files and an Incognito toggle on top, and Tools, Reasoning and Stream below the transcript. Incognito sends `metadata.private` (local servers only) and turns off feedback and saving. Reasoning is a prompt-level instruction.
- The old chat view is now **API** (section id `chat` is unchanged).
- The TUI no longer asks for a token at startup. **Keys** holds a gateway-token card (save, forget) above the provider keys; `Ctrl+K` jumps there.

### Added (runtime routing-policy edits)
- `PUT /v1/admin/policies/{name}` and `DELETE /v1/admin/policies/{name}` (admin): change one routing policy while the gateway runs. The body uses the same fields as `config/routing.yml` and is validated with the same rules; every problem is returned at once and an invalid edit never touches the active policies. Edits override file policies of the same name, survive file reloads, are marked in `GET /v1/admin/policies` (`runtime_edits`) and reset on restart. Audited as `policy.set` / `policy.remove`.
- `caips policy-set NAME [--strategy --weights --ceiling --hedge --chain --provider --model --fallback-model --allow --deny | --file]` and `caips policy-rm NAME`.
- TUI **Routing** section: policy table with runtime edits marked, Copy-to-fields, Set, Delete and a dry-run Explain against the selected policy.

### Added (workflow run history)
- Every workflow run (completed, failed, `needs_approval` or cost-capped) is recorded in the harness run store and the run response carries `run_id`. `GET /v1/workflows/{name}/runs` lists summary rows; `GET /v1/workflows/{name}/runs/{run_id}` returns the steps. Callers see their own runs; admins see all. Bounded: newest 200 per workflow, 90-day pruning, input and result truncated to 10 kB.
- `caips workflows runs NAME [--limit N]` and `caips workflows run-detail NAME RUN_ID`.

### Added (TUI budgets)
- **Budgets** section: one subject's budget window (limits, spent, resets in), with runtime Set and Remove against `PUT` / `DELETE /v1/admin/budgets/{subject}`.

### Changed (TUI)
- Dark and light themes (`caips-dark`, `caips-light`), toggled with Ctrl+T.
- Command palette (Ctrl+P): jump to any section or run refresh, set token, toggle theme, help.
- Sidebar status shows round-trip latency; connection loss and recovery raise a notification.
- Restyled sidebar, tables and focus states; brand header.

### Added (admin read API and CLI)
- `GET /v1/admin/budgets` (limits and current usage, optional `?subject=`) and `GET /v1/admin/policies` (active routing policies, reload count, last error). Admin scope, read-only.
- `caips explain PROMPT [--model] [--policy] [--provider]`: CLI for the `POST /v1/routing/explain` dry run.
- `caips policies`, `caips budgets [--subject]` and `caips audit [--limit] [--action]`. Editing budgets and policies from the CLI is not done.

### Added (key manager)
- Provider keys are no longer required at startup: the server starts with none and logs a warning. `/readyz` reports not ready until a provider exists.
- `/v1/keys` (admin): add, list and remove keys at runtime, and `GET /v1/keys/catalog`. Keys are encrypted at rest (`CAIPS_KEYSTORE`, `CAIPS_KEYSTORE_SECRET`), applied to the running router immediately, and never returned.
- Catalog of US labs (OpenAI, Anthropic, Google, xAI, Meta, NVIDIA, Perplexity), Chinese labs (DeepSeek, Alibaba Qwen, Moonshot, Zhipu, MiniMax, ByteDance, Baidu, Tencent, StepFun, 01.AI, Baichuan), plus `local` and `custom` servers. Labs without a native SDK use their OpenAI-compatible endpoint (`providers/compat.py`); endpoints are not tested against live services.
- Several keys per provider (`providers/pool.py`): rotation, with a 60 s cooldown for a key that gets an auth, quota or rate-limit error.
- TUI **Keys** section and `caips keys list|catalog|add|rm`.
- `Router.set_provider` / `drop_provider` for runtime changes.

### Changed
- Default port is now 6969 for the server, `caips`, `caips-tui`, the SDKs, Docker, Helm, Kubernetes and CI (was 8743 for the server and 8000 for clients).

### Changed (documentation refresh)
- README: describes the current feature set (OpenAI- and Anthropic-compatible APIs, tools and MCP, agent harness, local models, memory, RAG, budgets, metrics, CLI and SDKs, deploy assets), a full route table, more configuration variables, and `src/` layout commands.
- Documented that `/metrics` needs an admin token and that the CLI and SDKs default to port 8000 while the server uses 6969.
- `docs/api.md`: added a route index, corrected the response header table (removed `X-Response-Time`, which is not sent; added `X-CaiPS-Provider`, `X-CaiPS-Cost-USD`, rate-limit headers) and added 502 and 504 to the status codes.
- Added status notes to `docs/deployment.md`, `concepts.md`, `protocols.md` and `security.md` separating what is built from design material, and noted that SQL migrations are not run at startup.
- Fixed stale instructions in `docs/development.md`, `docs/runtime.md`, `docs/cli.md`, `docs/evals.md` and `docs/api.md`; added `docs/README.md` as an index.

### Added (terminal UI)
- `caips-tui` / `caips tui` (`src/caips_tui/`): a Textual terminal UI over the HTTP API. Chat in all three dialects (OpenAI `/v1/chat/completions`, native `/v1/completions`, Anthropic `/v1/messages`) with streaming, Esc to stop, `/retry`, routing explain, thumbs feedback and transcript export; plus sections for providers, models, usage, status/metrics, runs (with live follow, pause, approve, kill), tool approvals, tools (MCP call), agents, tasks, memory, RAG, feedback, local models, fine-tuning, evals and admin (audit, shutdown). Destructive actions ask first. See `docs/tui.md`.
- Optional extra: `pip install 'coreai-protocol-suite[tui]'`.
- API token is entered in the TUI (hidden prompt on first start, `Ctrl+K` to change) or via `caips login`, and saved per gateway in `~/.config/caips/credentials.json` (0600). `CAIPS_API_KEY` and `--api-key-file` still override it; the token is never a command-line flag.
- Not yet: key and budget management and routing-policy editing (no API for them yet), and a task list (the API has no list endpoint, so the Tasks section follows ids it has seen).

### Added (Phase 7: agent harness)
- `src/harness/`: tool-use loop with step, cost, token and deadline limits, context compaction with a pinned task, sqlite checkpoints after every step, human approvals, stall detection, kill switch and a per-run trace. Local-model agents get tighter caps.
- Run API (`api/runs_api.py`): `POST /v1/runs`, `GET /v1/runs[/{id}[/events|/stream]]`, `cancel`, `pause`, `resume`, and admin-only `POST /v1/runs/kill`. Opt-in: enabled by `config/agents.yml` (see `config/agents.example.yml`). Callers can only start agents whose tools they are granted; model calls count against the owner's budget; runs interrupted by a restart resume at startup.
- Not yet: Postgres run storage, agent memory, sub-agents, scheduled runs. `agents/emergency_shutdown.py` stays: it is the tested drain kill switch (`POST /v1/admin/shutdown`), distinct from the run kill switch.

### Added (Phase 4, part 1: OpenAI-compatible chat and models)
- `POST /v1/chat/completions` (`api/openai_compat.py`): OpenAI request and response shapes, SSE streaming with `stream_options.include_usage`, OpenAI error envelope, and an error event plus `[DONE]` (no `finish_reason`) when a stream fails midway. Budgets, usage ledger and metering apply as for `/v1/completions`. Verified with the official OpenAI Python SDK in tests, not against a live provider.
- Parameters that would change the answer (tools, `stop`, JSON mode, `n` above 1, images) are refused with `400 unsupported_parameter` instead of ignored; see `docs/api.md`.
- **Breaking:** `GET /v1/models` now returns the OpenAI list shape; the old registry response moved to `GET /v1/models/details`.
- Not yet: `/v1/embeddings`, `/v1/completions` (legacy OpenAI form; the existing `/v1/completions` is the CaiPS native one and unchanged), tool calling, structured output, vision, model aliases, `finish_reason: "length"`.

### Added (Phase 3, part 11: per-model budgets)
- `models:` section in `config/budgets.yml`: a shared cost/request/token cap per model across all subjects, `429` when reached. Checked up front only when the request names the model. Validated like the rest of the file.
- Not yet: per-team budgets (no team claim in tokens); audit entries for key changes (there are no key issue/revoke endpoints yet, so this belongs with Phase 10 scoped keys).

### Added (Phase 3, part 10: usage reports and routing docs)
- `GET /v1/usage/report`: usage over time (requests, tokens, cost) by hour or day, grouped by subject, provider and model, as JSON or CSV. Per-caller by default; `admin` sees all. Backed by a bounded in-memory ledger (`coreai/usage.py`, 30 days, 50,000 rows), so it resets on restart.
- `docs/routing.md`: what policy fields and weights mean, with worked examples.
- Not yet: persistent usage storage.

### Added (Phase 3, part 9: OpenTelemetry tracing)
- Optional tracing (`coreai/tracing.py`), off by default and costless when off. Enable with `COREAI_TRACING=1` or `OTEL_EXPORTER_OTLP_ENDPOINT`; install with `pip install 'coreai[tracing]'`. Spans: HTTP server span (route-template name, status), `caips.route`, `caips.provider_call` per attempt, `caips.stream`, with `failover` events. A caller's `traceparent` is continued. Sampling via `COREAI_TRACE_SAMPLE_RATIO`.
- Log lines written inside a span carry `trace_id` and `span_id`.
- Prompts, responses and exception messages never reach spans (tests fail if they do). Export is batched with a bounded queue.
- `Router.route` and `Router.stream` are now thin wrappers over `_route` and `_stream`; behaviour is unchanged.
- `pyproject.toml`: new `tracing` extra; the `dev` extra includes `opentelemetry-sdk` so CI runs the tracing tests.
- Not yet: trace context is not passed on to providers or through the agent orchestrator. Verified against an in-memory exporter only, not a live collector.

### Added (Phase 3, part 8: streamed responses are metered)
- Streams now have token counts and cost. Providers report usage from their SDKs (OpenAI, Grok and DeepSeek via `stream_options.include_usage`; Anthropic from the final message; Gemini from usage metadata, thinking tokens included). If a provider reports nothing, the router estimates from text length and flags it (`caips_stream_usage_estimated_total`).
- The router settles every stream that delivered output, however it ended (finished, failed midway, client left): provider spend, tokens in `caips_tokens_total`, and a `stream settled` log line.
- Stream cost and tokens count toward per-key budgets (the request itself is still counted once, at the start). Streams are not added to the latency histogram, so a long generation cannot trip the latency alert.
- New `StreamUsage` type and `CompletionRequest.stream_usage` field; `BudgetManager.add_usage`.
- The streaming request options and usage fields were written from the SDKs' documented behaviour and tested against fakes. They have not been run against the live provider APIs.

### Changed (Phase 3, part 7: cost tracking)
- **One price table** (`config/pricing.yml`, loaded and validated by `coreai/pricing.py`) replaces five per-provider copies and a sixth, diverging, one in `utils/cost.py` (for example `gemini-1.5-pro` was $1.25/$5 in one and $3.50/$10.50 in another). Prices are per million tokens. Values were carried over unchanged from the provider modules and are marked `reviewed: null`; they have not been re-checked against vendor price pages.
- Unlisted models are still billed at the provider default, but now log a warning once and are counted in `caips_unpriced_requests_total`. Routing estimates are not counted, only billed responses.
- Prices that look wrong are logged at startup; `gemini-2.5-pro` (input priced above output) is flagged for verification.
- `utils.cost` keeps its API (`PRICING`, `calculate_cost`, ...) as a facade over the shared table.
- `BudgetTracker` fixes: a limit of `0` was ignored (truthiness checks) in `record`, `would_exceed` and `stats`.
- The provider modules' `*_PRICING` constants are gone; use `coreai.pricing.get_pricing()`.

### Added (Phase 3, part 6: structured logs and request IDs)
- **JSON logs through one pipeline** (`coreai/logging_setup.py`): loguru as JSON lines (or `COREAI_LOG_FORMAT=text`), standard-library logging (uvicorn included) routed into it, bearer tokens and `sk-` keys masked, optional log file with size rotation and a retention count (`COREAI_LOG_FILE`, `COREAI_LOG_ROTATION`, `COREAI_LOG_RETENTION`). `structlog` was never used and is not needed.
- **Request IDs** (`coreai/request_context.py`): a pure-ASGI middleware gives every request an ID (a safe client `X-Request-ID` is kept), puts it on every log line and in the response header, including drain 503s, and writes one access line per request.
- The router's per-call log line is now structured (`provider`, `model`, tokens, `cost_usd`, `latency_ms`, `failed_over_from`, `hedged`), so "which provider served this request, and what did it cost" is answerable from logs. See `docs/operations.md`.
- Log output format changed from text to JSON by default; set `COREAI_LOG_FORMAT=text` for the old style.

### Removed (Phase 3, part 6)
- `RequestLoggerMiddleware`: it was never added to the app. Replaced by the middleware above.

### Not yet
- Passing the request ID to providers, and the OpenTelemetry trace ID in logs.

### Added (Phase 3, part 5: dashboard and alerts)
- `deploy/grafana/caips-dashboard.json`: Grafana dashboard for the gateway metrics.
- `deploy/prometheus/caips-alerts.yml`: alert rules for open circuits, error rate, all providers down, cost spikes, latency, draining and failed scrapes.
- `tests/test_observability_assets.py` keeps both in step with `/metrics`: they parse, panels don't overlap, and every metric they use is exposed. Not yet loaded into a live Grafana or checked with `promtool`. Scrape setup and token note in `docs/operations.md`.

### Added (Phase 3, part 4: latency histograms and token counts)
- `/metrics` now exposes `caips_request_duration_seconds` (a real Prometheus histogram: buckets from 50ms to 30s plus `+Inf`, `_sum` and `_count`) and `caips_tokens_total` (input and output), both labelled by `provider` and `model`. The router keeps these counters (`Router.model_stats`, also in `stats()` as `model_stats`).
- Model labels are capped at 50 per provider; further models share `model="other"`, so a stream of odd model names can't grow memory or metric cardinality.
- Not yet: per-model cost, error and cache metrics per model.

### Added (Phase 3, part 3: audit log)
- **Audit log** (`coreai/audit.py`): records kill-switch triggers, routing policy loads and hot reloads (accepted and rejected) and budget config loads, with actor, target and outcome. `GET /v1/admin/audit` (admin scope) reads it. Bounded in memory; optional JSONL file via `COREAI_AUDIT_FILE` with size-capped rotation; secret-looking fields are redacted. See `docs/operations.md`.
- `PolicyStore.on_event` callback reports reload outcomes without ever breaking a reload.
- Not yet: key and token changes (no key-management endpoints exist yet).

### Added (Phase 3, part 2: budgets and quotas)
- **Per-subject budgets** (`coreai/budgets.py`): hard limits on cost, requests and tokens per window, plus a soft cost warning. Over-limit requests get `429` with `Retry-After`. Configure with `config/budgets.yml` or `COREAI_BUDGET_FILE` (example in `config/budgets.example.yml`; off without a file; an invalid file stops startup). `GET /v1/usage` shows the caller's usage. See `docs/budgets.md`.
- Not yet: per-team and per-model budgets and persistence across restarts.

### Added (Phase 3, part 1: probes and metrics)
- `GET /healthz` (liveness, no dependency checks) and `GET /readyz` (readiness: 503 while draining, before the router exists, or when every provider's circuit is open). Both stay reachable during a drain.
- `GET /metrics` in Prometheus text format (admin scope): requests, failovers, hedges, per-provider requests, errors by kind, cost, p50 latency, health, circuit state and opens, cache hits and misses, accepting-traffic and in-flight gauges. Rendered by `coreai/metrics.py` from `Router.stats()`, with no new dependency.

### Added (Phase 2, part 6: chaos and load checks)
- `scripts/chaos.py`: kills, slows or breaks each provider in turn (down, 500, 529, 429, flaky, slow) and checks the gateway keeps serving at 99%+, stays within a p95 bound and gets traffic back to the provider after recovery; plus an all-providers-down scenario. Runs in CI.
- `scripts/loadtest.py`: sustained load with `tracemalloc`; fails on memory growth, unbounded caches or histories, or errors. Runs in CI.
- Behavior documented in `docs/operations.md`.

### Fixed (Phase 2, part 6)
- **`rate_limits` in `config/routing.yml` were ignored.** The router gave every provider a hard-coded 100 requests and 90,000 tokens per minute, so the gateway throttled itself far below configured limits. Limits are now read from `providers.<name>.rate_limits` (validated), and `RoutingConfig(rate_limits=...)` sets them in code.
- CI lint (flake8, isort) failures in files from earlier Phase 2 parts.

### Added (Phase 2, part 5)
- **Optional hedged requests.** With `hedge_after_seconds` (or a policy's `hedge_after_ms`), a request still unanswered after that delay is also sent to the next provider; the first success wins and the other call is cancelled. Off by default. Never used for pinned providers, pinned models or streams. A cancelled loser doesn't count as a failure; a provider tried as a hedge isn't retried in normal failover. Responses carry `metadata["hedged"]`, and `/v1/stats` reports `hedges.launched` and `hedges.won_by_second`. `low_latency` in `config/routing.yml` now hedges after 800ms. Note: a hedge can mean paying for two completions.

### Added (Phase 2, part 4)
- **Per-provider health scoring** (`coreai/health.py`): a time-decayed success rate (0 to 1, 60s half-life by default) from recent outcomes. The router adds `health_weight * (1 - health)` to the balanced score (default weight 0.5; set `health_weight=0` to turn it off), so failing providers are tried later and recover as old failures age out. Caller errors (HTTP 400-class) don't count against a provider. Health is shown in `/v1/stats`, `GET /v1/providers` and the `/v1/routing/explain` trace. It applies to balanced/cost-aware/cheapest ordering; `latency_first` and `fallback` orderings are unchanged.

### Added (Phase 2, part 3)
- **Tested kill switch.** `POST /v1/admin/shutdown` now closes intake first (new requests get 503 with `Retry-After`; `/health` and `/v1/admin/*` stay reachable), waits for in-flight requests (streams included) up to a bounded time, then tears down agents, kernel and database. `GET /v1/admin/shutdown` reports `accepting`, `in_flight` and the last shutdown event. Forced mode closes intake but does not wait.
- `coreai/drain.py`: `DrainController` and a pure-ASGI `DrainMiddleware`.

### Fixed (Phase 2, part 3)
- The shutdown endpoint built a new `EmergencyShutdown` on every call, so the duplicate-trigger guard never worked; a second call now returns 409. It also raised `SystemExit` inside a background task; the server now exits through SIGTERM so uvicorn finishes cleanly.

### Added (Phase 2, part 2)
- **Declarative routing policies.** `config/routing.yml` `policies:` are now read by `coreai/policy.py`: strategy, cost/latency weights, `latency_ceiling_ms`, `allow`/`deny` lists, `fallback_chain` and an `explicit` pinned provider. Files are validated (all problems reported at once) and hot-reloaded; an invalid edit keeps the last good policies. Select a policy per request with `metadata["policy"]`; otherwise the policy named `default` applies. Without a policy file, routing is unchanged.
- **Dry run:** `POST /v1/routing/explain` (and `Router.explain()`) returns the provider order and, per provider, whether it is eligible, why not, and its score. It sends nothing and changes no routing state.
- Set `COREAI_ROUTING_POLICY_FILE` to use a policy file other than `config/routing.yml`.
- Not yet applied: a policy's `model` / `fallback_model` fields are validated but the router does not substitute models yet.

### Fixed
- **There was no failover.** `route()` picked one provider and raised on failure; the `fallback` strategy never fell back. Failed requests now move to the next-best provider.
- **Every Anthropic call failed** with the current SDK (1.x): the provider passed `temperature`, which `messages.create()` no longer accepts. It is no longer sent.
- **HTTP 529 (Anthropic overloaded) was never retried.** Retry decisions used message-text matching; they now use HTTP status and exception type, with jitter, `Retry-After` and a request deadline.
- The SDK clients had a 10-minute default timeout and their own hidden retries stacked under the gateway's. They now have explicit timeouts and `max_retries=0`.
- The rate limiter never deducted tokens after a wait (over-admitting), could wait forever, and could be raced past its concurrency limit. Waits are now bounded by the deadline and budgets are reserved and refunded.
- The in-memory response cache and the finished-task store grew without bound; both are now size-limited.
- `GET /v1/providers` always reported every provider unavailable (it called `ping()` methods that do not exist). It now reports circuit-breaker state and recent errors.
- Every error was returned as HTTP 500 with the raw upstream message. Errors now map to 400, 429, 502, 503 or 504 without leaking upstream text. Streaming now goes through the router, so streams get breakers, limits and failover.

### Added
- Circuit breakers per provider, per-provider timeouts and a shared request deadline, configurable by environment variable (see `.env.example`).
- `failovers`, per-provider `error_kinds` and breaker state in `/v1/stats` and `/v1/providers`.
- About 175 new tests for the reliability layer, including contract tests that run the real provider classes over mock HTTP transports.

### Removed
- The `tenacity` dependency (retry is now a single small implementation).

### Changed
- Runtime dependencies cut from 50+ to 15 (only what the code imports). Dev tools are the `dev` extra; Postgres support is the `postgres` extra.
- One `pyproject.toml` replaces `setup.py`, `setup.cfg` and `pytest.ini`. Wheels now include all packages (`agents`, `neural`, `protocols` and `database` were previously missing from non-editable installs).
- CI runs on Python 3.11 and 3.12, builds and imports the wheel, and no longer contains hardcoded keys.
- README rewritten to describe what the code does today.

### Removed
- `neural/consciousness_detection.py` and its wiring in `AgentManager` (memory leak, never exposed by the API).
- Tracked logs, database dumps, Terraform state, shell history and two ad-hoc ops scripts (see `docs/operations.md`).

### Added
- `.env.example`, `SECURITY.md`, status notes in `agents/`, `protocols/` and `neural/`, and smoke tests for `AgentManager`.

## Unreleased (roadmap Phase 4 / Phase 0 follow-ups)
- Normalised tool calling: `tools`, `tool_choice`, assistant `tool_calls` and `tool`
  messages on `/v1/chat/completions` for OpenAI, Grok, DeepSeek, Anthropic and Gemini
  (non-streaming). Structured output via `response_format` `json_object` / `json_schema`.
  The model catalogue advertises `supports_function_calling` again (False for
  `deepseek-reasoner`). Tool requests bypass the cache; the cache key now includes
  `response_format`. Streaming with tools is refused with a 400.
- Added image inputs (`image_url` parts) to `/v1/chat/completions` for OpenAI, Grok,
  Anthropic and Gemini; text-only models and providers are skipped or refused.
- Added the Anthropic-compatible `POST /v1/messages` (text, images, tools, streaming
  of text), checked against the official `anthropic` SDK.
- Lint: ruff and black replace flake8 and isort; mypy baseline for `providers/base.py`.
- Runtime dependencies cut from 14 to 11; `redis` is now an optional extra.
- Docker image: no apt layer, healthcheck uses Python instead of curl.
- Added `/v1/embeddings` (OpenAI provider) and virtual models `fast`, `cheap`, `best`
  mapped to the `low_latency`, `bulk` and `high_quality` routing policies; they are
  listed by `/v1/models`.
- Untracked `.env`, `.env.production` and `config/secrets.json`; CI now runs gitleaks
  and `pip-audit`.

## Unreleased
- Add CONTRIBUTING.md and examples/ (Phase 4 developer experience).
- Make `tests/test_provider_contracts.py` work with SDK versions exposing `httpx2`.
- Streaming requests with tools or a JSON schema are buffered and sent as SSE chunks.
- `response_format: json_schema` output is validated; one retry, then 502 `schema_validation_failed`.
- `src/` layout, `py.typed`, mypy ratchet in `pyproject.toml`, pre-commit hooks.
- Separate `migrations/postgres` and `migrations/sqlite` with down scripts; roundtrip tests.
- OpenAI SDK compatibility tests, `docker-compose.yml`, quickstart CI job, PyPI publish workflow.
- Phase 5: local OpenAI-compatible servers (`providers/local.py`, `config/local.yml`), model discovery, capability flags, `private` routing.
