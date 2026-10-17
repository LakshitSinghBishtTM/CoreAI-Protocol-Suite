# Operations

This is a stub for the incident runbook (roadmap Phase 3). It replaces two ad-hoc
shell scripts that were removed from `scripts/` in Phase 0: `fix_server.sh` and
`emergency_fix.sh`. They remain in git history if you need to see what they did.

The operations they covered, to be rewritten as documented, tested procedures:

| Operation | Old script | Planned replacement |
| --- | --- | --- |
| Restart the API service | fix_server.sh `restart` | Documented systemd/Docker commands |
| Flush the Redis cache | fix_server.sh `flush-cache` | Admin API endpoint with audit log |
| Rotate and compress logs | fix_server.sh `rotate-logs` | Built-in log rotation with size caps |
| Kill stuck agent processes | fix_server.sh `kill-agents` | Agent kill switch (Phase 7) |
| Health checks | fix_server.sh `check` | `/healthz` and `/readyz` endpoints (Phase 3) |
| Hard restart | emergency_fix.sh `hard-restart` | Drain-then-restart procedure |
| Roll back a deploy | emergency_fix.sh `rollback` | Versioned releases and documented rollback |
| Disable a provider at runtime | emergency_fix.sh `disable-provider` | Routing policy / admin API |
| Clear hung DB connections | emergency_fix.sh `clear-db-locks` | Connection pool timeouts |
| Full state reset | emergency_fix.sh `full-reset` | Documented, confirmed, backed-up procedure |

## To write

- Provider outage
- High latency
- Cost spike
- Memory growth
- Local model process crash (after the local runtime lands)

## Gateway failure behavior (Phase 2)

What the gateway does when a provider misbehaves. Every row is covered by a test, and `scripts/chaos.py` checks the end-to-end behavior.

| Provider fault | Gateway response |
| --- | --- |
| Connection refused, 5xx, timeout | Retry once on the same provider (bounded, jittered backoff), then fail over to the next provider. After `CIRCUIT_FAILURE_THRESHOLD` consecutive failures its breaker opens and it is skipped. |
| 529 / 503 overloaded | Same as above; `Retry-After` is honored when it fits the request deadline. |
| 429 rate limited | Same; `Retry-After` honored. Quota errors are not retried. |
| Very slow responses | Cut off at the per-provider timeout (`PROVIDER_TIMEOUTS`), then failover. Optional hedging asks the next provider after `hedge_after_seconds`. |
| Flaky (some calls fail) | Failures lower the provider's health score, so it is tried later; the score recovers as old failures age out. |
| One provider down | Other providers serve; breaker probes the failed one every `CIRCUIT_RESET_SECONDS`; traffic returns after a successful probe. |
| Every provider down | Requests fail fast with HTTP 503, an `all_providers_unavailable` body and `Retry-After`. Service resumes after the first successful probe. |
| 400-class error | Returned to the caller; never retried, never failed over, never counts against the provider. |

While every breaker is half-open, each admits one probe at a time, so a burst arriving at that instant sees fast 503s for a few milliseconds. Clients should retry on 503.

Pinned requests (a named provider or model) are never failed over, by design.

### Chaos check

```bash
python scripts/chaos.py            # all providers x all faults, about 40s
python scripts/chaos.py --json
```

For each provider and each fault (`down`, `http_500`, `overloaded_529`, `rate_limited_429`, `flaky_50pct`, `slow`) it checks that at least 99% of requests still succeed while the fault is active, that p95 latency stays bounded, and that the provider gets traffic back after the fault ends. A final scenario takes every provider down. It uses fake providers, so it needs no keys and no network, and it runs in CI.

### Load test and memory

```bash
python scripts/loadtest.py                         # 20,000 requests
python scripts/loadtest.py --requests 100000 --max-growth-mb 5
```

Sends unique prompts (so the cache keeps filling) through three providers, one of them flaky, with `tracemalloc` on. It fails if memory grows more than `--max-growth-mb` after warm-up, if the cache or any history exceeds its limit, or if more than 1% of requests fail. On failure it prints the source lines that grew. Reference result on a laptop-class machine: 15,000 requests after warm-up, 0% errors, 0.01MB growth, cache held at its 500-entry limit.

Throughput printed by this script is for the routing layer only (fake providers, tracing on); it is not a capacity figure for a real deployment.

### Per-provider rate limits

`providers.<name>.rate_limits` in `config/routing.yml` (`rpm`, `tpm`, optional `concurrent`) now sets the gateway's own limits. Before this change those values were ignored and every provider was capped at 100 requests and 90,000 tokens per minute. Providers with no entry keep those conservative defaults, so set limits for every provider you use.

### Kill switch

`POST /v1/admin/shutdown?mode=graceful|immediate|forced` (admin scope). New requests get 503 immediately; graceful and immediate wait for in-flight requests (up to about 24s and 5s), then stop agents, flush the kernel and database, and exit through SIGTERM. A second call returns 409. `GET /v1/admin/shutdown` shows `accepting`, `in_flight` and the last event.

### Memory growth

Every cache, history and task store is bounded (see `tests/test_bounded_state.py`); `scripts/loadtest.py` is the regression check. If memory grows in production, run the load test with `--requests 100000` first, then profile the live process with `memray`.
