# Changelog

## 0.1.0 (unreleased)

The previous `1.0.0` label did not match the project's state; versions now start at 0.1.0 (alpha).

### Added (Phase 3, part 10: usage reports and routing docs)
- `GET /v1/usage/report`: usage over time (requests, tokens, cost) by hour or day, grouped by subject, provider and model, as JSON or CSV. Per-caller by default; `admin` sees all. Backed by a bounded in-memory ledger (`coreai/usage.py`, 30 days, 50,000 rows), so it resets on restart.
- `docs/routing.md`: what policy fields and weights mean, with worked examples.
- Not yet: per-team and per-model budgets; persistent usage storage; audit entries for key changes.

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
