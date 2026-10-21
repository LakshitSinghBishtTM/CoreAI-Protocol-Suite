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

## Audit log

Administrative and configuration events are recorded with who, what, when and
the outcome: kill-switch triggers (including rejected duplicates), routing
policy loads and hot reloads (including rejected edits, with the problems), and
budget config loads.

- Read it with `GET /v1/admin/audit?limit=100&action=policy&actor=user:x`
  (admin scope, newest first). `action` matches exactly or by prefix
  (`policy` matches `policy.reload`).
- It lives in a bounded in-memory ring (last 1000 events, lost on restart). Set
  `COREAI_AUDIT_FILE` to also append JSON lines to a file; it rotates at 5 MB
  to `<file>.1`, keeping at most two files.
- Detail fields whose names contain `key`, `token`, `secret`, `password`,
  `authorization` or `credential` are redacted, and long values are cut at 500
  characters.
- A failure to write the file never affects requests; `write_errors` counts
  them.
- Not yet recorded: API key and token changes (the gateway has no key
  management endpoints yet) and budget changes made at runtime (budgets load
  only at startup).

## Dashboard and alerts

- `deploy/grafana/caips-dashboard.json`: import it in Grafana and pick your
  Prometheus data source. Panels cover request rate, spend per hour, traffic
  state, health, circuit breakers, latency p50/p95/p99, errors by kind,
  failovers and hedges, tokens and cache hit ratio.
- `deploy/prometheus/caips-alerts.yml`: rules for an open circuit, high error
  rate, every provider down, a cost spike (above $1/hour and 3x the last
  hour), high p95 latency, the gateway draining, and a failed scrape. With a
  15s scrape interval each rule fires within about a minute.
- `/metrics` needs an admin-scope bearer token, so give Prometheus one:

  ```yaml
  scrape_configs:
    - job_name: caips
      scrape_interval: 15s
      metrics_path: /metrics
      authorization:
        credentials_file: /etc/prometheus/caips_token   # a JWT with scope "admin"
      static_configs:
        - targets: ["caips:8743"]
  ```

JWTs expire (60 minutes by default, `JWT_EXPIRY_MINS`), so a long-lived scraper
token needs a longer expiry or a refresh job until scoped, expiring API keys
exist (Phase 10).

Tests check that the dashboard and rules parse and only use metrics the
gateway exposes. They have not been loaded into a live Grafana or checked with
`promtool` yet, so import and review before relying on them.

## Logs and request IDs

Logs are JSON lines on stderr, one object per line, through a single pipeline
(loguru; standard-library `logging`, including uvicorn, is routed into it).

- Every response carries `X-Request-ID`, and every log line written while the
  request is served carries the same `request_id`. A client-supplied
  `X-Request-ID` is kept if it is 8 to 64 characters of letters, digits, `.`,
  `_` or `-`; otherwise a new ID is generated.
- One `request` line per request: `method`, `path`, `status`, `duration_ms`
  (WARNING for 5xx). Probe and scrape paths (`/health`, `/healthz`, `/readyz`,
  `/metrics`) are not logged.
- One `provider response` line per successful call: `provider`, `model`,
  `input_tokens`, `output_tokens`, `cost_usd`, `latency_ms`,
  `failed_over_from` (providers tried first and why they were skipped are in
  the failover logs) and `hedged`. To see which provider served a request and
  what it cost: `jq 'select(.request_id=="<id>")'`.
- Bearer tokens and `sk-` style keys are masked in messages and exception
  text. Prompts and responses are never logged.

| Variable | Default | Meaning |
| --- | --- | --- |
| `COREAI_LOG_FORMAT` | `json` | `json` or `text` (for local development) |
| `COREAI_LOG_LEVEL` | `INFO` | minimum level |
| `COREAI_LOG_FILE` | unset | also write to this file |
| `COREAI_LOG_ROTATION` | `50 MB` | rotate the file at this size |
| `COREAI_LOG_RETENTION` | `5` | rotated files kept |

With a log file, disk use is bounded at roughly `rotation x (retention + 1)`.

Not yet done: passing the request ID to providers that accept a client
request header, and the OpenTelemetry trace ID in log lines.

## Model prices

All cost figures (response `cost_usd`, spend counters, budgets, cost-aware
routing) come from one file, `config/pricing.yml`, in USD per million tokens.
It is validated at startup: a bad file stops the gateway and lists every
problem. Use `COREAI_PRICING_FILE` to point at another file.

- **Keeping it current:** when a vendor changes a price, edit the file, bump
  `version`, and set `reviewed` to the date you checked the vendor's price page.
  The shipped file has `reviewed: null` and logs a warning at startup, because
  its numbers were carried over unchanged from the old per-provider tables and
  have not been re-checked.
- **Unlisted models** are billed at the provider's `default` rate so requests
  still work, with a one-time warning and the metric
  `caips_unpriced_requests_total{provider,model}`. A non-zero value means
  `cost_usd` for that model is a guess: add it to the file.
- Prices that look wrong (output priced below input) are logged at startup.
  Currently `gemini/gemini-2.5-pro` is flagged; verify it.
- Changes need a restart (prices are not hot-reloaded).

## Tracing (OpenTelemetry)

Optional and off by default; when off it costs nothing. Install the extra and
turn it on:

```
pip install 'coreai[tracing]'
COREAI_TRACING=1 OTEL_EXPORTER_OTLP_ENDPOINT=http://collector:4318 uvicorn ...
```

Setting `OTEL_EXPORTER_OTLP_ENDPOINT` (or `..._TRACES_ENDPOINT`) also turns it
on. Spans are sent over OTLP/HTTP and honour the standard `OTEL_EXPORTER_OTLP_*`
variables (headers, timeout, protocol settings). Also read:

| Variable | Default | Meaning |
| --- | --- | --- |
| `COREAI_TRACING` | off | `1`, `true`, `yes` or `on` to enable |
| `OTEL_SERVICE_NAME` | `caips-gateway` | service name on every span |
| `COREAI_TRACE_SAMPLE_RATIO` | `1` | fraction of new traces kept, 0 to 1 |

A caller's W3C `traceparent` is continued, and its sampling decision wins, so a
sampled caller is always traced even at ratio 0.

**Spans**

- `GET /v1/providers` style server span per HTTP request, named by route
  template (not the raw path), with status code and `caips.request_id`. 5xx
  responses are marked as errors.
- `caips.route` per completion: `caips.provider`, `caips.model`, token counts,
  `caips.cost_usd`, `caips.cached`, `caips.failed_over_from`, `caips.hedged`,
  plus a `failover` event (from, to, error kind) each time it moves on.
- `caips.provider_call` per provider attempt, a child of `caips.route`; failed
  ones carry `caips.error_kind`.
- `caips.stream` per streamed completion, with the settled usage and cost
  (`caips.usage_estimated`, `caips.stream_completed`).

Every log line written inside a span carries `trace_id` and `span_id`, so a
slow trace links to its logs, and `request_id` links the logs to the response
header.

**Privacy and safety**

- Prompts, responses and exception messages are never put on spans. Errors carry
  the exception type and the gateway's error kind only.
- Export is batched with a bounded queue that drops on overflow, so a slow or
  down collector cannot grow memory or slow requests.
- A missing package or bad setting logs an error and leaves tracing off; it
  never stops the gateway. Spans are flushed on shutdown.

Not yet: trace context is not passed on to providers, and it is not carried
through the agent orchestrator.

## Budget alerts

Set `COREAI_ALERT_WEBHOOK_URL` and `COREAI_ALERT_WEBHOOK_SECRET` (the secret is required) and the gateway POSTs a JSON event the first time a subject or model crosses its `warn_cost_usd` (`budget.warning`) or hits a hard limit (`budget.exceeded`) in each budget window. Verify with `X-CaiPS-Signature = HMAC-SHA256(secret, "<X-CaiPS-Timestamp>.<body>")`. Events carry the subject or model and usage numbers, never prompts or keys. Delivery is best effort: a failed webhook is logged and never blocks requests.
