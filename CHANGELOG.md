# Changelog

## 0.1.0 (unreleased)

The previous `1.0.0` label did not match the project's state; versions now start at 0.1.0 (alpha).

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
