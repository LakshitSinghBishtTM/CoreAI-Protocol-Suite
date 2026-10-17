# Changelog

## 0.1.0 (unreleased)

The previous `1.0.0` label did not match the project's state; versions now start at 0.1.0 (alpha).

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
