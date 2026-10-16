<p align="center">
  <img alt="CoreAI-Protocol-Suite Logo" src="assets/logo.svg" width="260">
</p>

<h1 align="center">CoreAI Protocol Suite (CaiPS)</h1>
<p align="center">A multi-provider LLM gateway with routing, failover, caching and rate limiting. Alpha.</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-GPL--3.0-blue.svg" alt="License"></a>
  <a href="https://github.com/LakshitSinghBishtTM/CoreAI-Protocol-Suite/actions/workflows/ci.yml"><img src="https://github.com/LakshitSinghBishtTM/CoreAI-Protocol-Suite/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/badge/status-alpha-orange" alt="Status: alpha">
  <a href="https://github.com/LakshitSinghBishtTM/CoreAI-Protocol-Suite/graphs/contributors"><img src="https://img.shields.io/github/contributors/LakshitSinghBishtTM/CoreAI-Protocol-Suite?color=green" alt="Contributors"></a>
</p>

---

CaiPS sits between your application and AI model providers. You send requests to one API, and CaiPS decides which provider handles each one, retries on failure, caches responses, and enforces rate limits.

It is **alpha software**. The gateway core works and is unit tested. Several other parts are experimental or not wired in yet, and the table below says which.

## What works today

- **Providers:** OpenAI, Anthropic, Gemini, Grok and DeepSeek behind one interface.
- **Routing strategies:** `cheapest`, `fastest`, `balanced` (weighted cost and latency), `round_robin` and `fallback` (providers in registration order).
- **Failover:** if a provider fails with overload (429, 503, 529), a 5xx, a timeout or a connection error, the request moves to the next-best provider. Requests that pin a provider or a specific model do not fail over. Streams fail over only before the first chunk is sent.
- **Circuit breakers:** a provider that keeps failing is skipped for a cooldown, then probed with a single request before it takes traffic again.
- **Timeouts and deadlines:** a per-provider timeout for each attempt, and one request deadline shared by all retries and failovers.
- **Response caching:** in-memory (LRU, size-limited) or Redis.
- **Rate limiting:** per provider (requests, tokens and concurrency) and global, plus a sliding-window limiter on the API keyed by API key or client IP. Waiting is bounded by the request deadline.
- **Retries:** exponential backoff with jitter for transient errors only; honors `Retry-After`; never retries bad requests or auth errors; stops when the deadline would be exceeded.
- **Auth:** JWT bearer tokens or `X-API-Key`, with per-endpoint scopes.
- **Streaming completions** and a **background task queue**.
- **Model registry:** capability, context window and price data per model.
- **Fine-tuning job API** (OpenAI only).
- **Storage:** SQLite by default, optional Postgres.

## Status by component

| Component | Status | Notes |
| --- | --- | --- |
| Router, failover, circuit breaker, retry, limiter | Alpha | Tested with simulated failures, and with the real Anthropic, OpenAI and Gemini SDK error types over mock HTTP transports. Not yet exercised against a live provider outage. |
| Providers and cache | Alpha | Anthropic and OpenAI have request/response contract tests; Gemini, Grok and DeepSeek do not yet. |
| API server and auth | Alpha | Error mapping, streaming and provider health are tested; roughly two thirds of the route code is covered. |
| Task orchestrator | Alpha | Queue and dispatch exist; limited tests. |
| Model registry (`neural/model.py`) | Alpha | Static table; entries and prices go stale. |
| Fine-tuning API (`neural/training.py`) | Optional | OpenAI only; Anthropic is a stub. |
| Agents (`agents/`) | Experimental | Lifecycle management only. **Agents have no tools yet.** See `agents/README.md`. |
| Distributed and secure protocols (`protocols/`) | Experimental | Not used by the request path. The custom crypto is **unaudited**; do not rely on it. See `protocols/README.md`. |
| Cost tracking (`utils/cost.py`) | Not wired in | Code exists but the server does not call it yet. |
| Request logging and validation (`middleware/`) | Not wired in | The server uses `api/middleware.py`. |
| SQL migrations (`migrations/`, `database/migrations.py`) | Not wired in | Not run at startup. |

## Quickstart

Requires Python 3.11 or newer.

```bash
git clone https://github.com/LakshitSinghBishtTM/CoreAI-Protocol-Suite
cd CoreAI-Protocol-Suite
pip install -e .

export SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
export OPENAI_API_KEY="sk-..."   # set at least one provider key

uvicorn api.server:app --port 8743
```

The server refuses to start if no provider key is set. It reads real environment variables and does **not** load `.env` files itself. To keep settings in a file, copy `.env.example` to `.env`, fill it in, and run `set -a; source .env; set +a` before starting (Docker users can pass `--env-file .env`).

There is no token-issuing endpoint yet, so mint a token yourself with the same `SECRET_KEY`:

```bash
TOKEN=$(python -c "from api.auth import create_access_token; print(create_access_token('user:me', ['completions:read', 'completions:write']))")

curl localhost:8743/health
curl -H "Authorization: Bearer $TOKEN" localhost:8743/v1/providers
curl -X POST localhost:8743/v1/completions \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"Hello"}]}'
```

### Docker

```bash
docker build -t caips .
docker run -p 8743:8743 --env-file .env caips
```

## API

All routes except `/health` need authentication. Scope required is in brackets.

| Method and path | Purpose |
| --- | --- |
| `GET /health` | Liveness and loaded providers (no auth) |
| `POST /v1/completions` | Completion through the router `[completions:write]` |
| `POST /v1/completions/stream` | Streaming completion `[completions:write]` |
| `GET /v1/providers` | Provider health from circuit breakers and recent errors (no network calls) `[completions:read]` |
| `GET /v1/models` | Model registry `[completions:read]` |
| `POST /v1/tasks`, `GET` and `DELETE /v1/tasks/{id}` | Background tasks `[tasks:read, tasks:write]` |
| `POST /v1/agents`, `GET /v1/agents`, `DELETE /v1/agents/{id}` | Experimental agents `[agents:read, agents:write]` |
| `GET /v1/stats` | Runtime statistics `[admin]` |
| `POST /v1/admin/shutdown` | Shutdown `[admin]` |
| `POST`, `GET` and `DELETE /v1/finetune/jobs` | Fine-tuning jobs `[admin]` |

Interactive docs are at `/docs` while the server runs.

### Error responses

Provider failures are translated into meaningful status codes. Upstream error text is logged but never returned (it can contain request details or partial credentials).

| Status | Meaning |
| --- | --- |
| 400 | Invalid request: unknown provider, context window exceeded, or the provider rejected the input |
| 429 | Provider rate limit reached; `Retry-After` is set when the provider sent one |
| 502 | Provider unreachable or returned a 5xx; or the gateway's key or quota was rejected |
| 503 | Provider overloaded, or every provider failed or is circuit-broken; `Retry-After` when known |
| 504 | Provider timed out, or the request deadline was exceeded |

A stream that fails before its first chunk is returned as one of these errors. A stream that fails after output has started ends with a final `[stream_error] <kind>` chunk.

## Configuration

See `.env.example` for a template. The main variables:

| Variable | Purpose |
| --- | --- |
| `SECRET_KEY` | Signs JWTs. Required. |
| `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `GROK_API_KEY`, `DEEPSEEK_API_KEY` | Enable each provider. At least one is required. |
| `DATABASE_URL` | Postgres URL. Empty means SQLite (install `.[postgres]` for Postgres). |
| `DB_PATH` | SQLite file location. |
| `JWT_EXPIRY_MINS` | Token lifetime. |
| `REQUEST_TIMEOUT_SECONDS` | Per-attempt timeout for one provider call (default 60). |
| `PROVIDER_TIMEOUTS` | Per-provider overrides, for example `deepseek=120,grok=90`. |
| `REQUEST_DEADLINE_SECONDS` | Total budget across retries and failovers (default 120; `none` disables). |
| `ENABLE_FAILOVER` | Set `false` to disable failover (default `true`). |
| `MAX_ATTEMPTS_BEFORE_FAILOVER` | Tries on one provider while others remain (default 2). |
| `CIRCUIT_FAILURE_THRESHOLD`, `CIRCUIT_RESET_SECONDS` | Consecutive failures that stop traffic to a provider (default 5), and the cooldown before it is probed again (default 30). |
| `CACHE_MAX_ENTRIES` | Size limit of the in-memory response cache (default 1000). |
| `WEBHOOK_SIGNING_SECRET` | Verifies signed webhook requests. |

## Development

```bash
pip install -e .[dev]
pytest
flake8 coreai/ api/ agents/ && black --check coreai/ api/ agents/ && isort --check-only coreai/ api/ agents/
```

CI runs lint, tests on Python 3.11 and 3.12, a wheel build-and-import check, and a Docker build with a health check.

## Known limitations

- Failover is verified with simulated failures, not yet against a real provider outage. Circuit breaker state is per process, so replicas do not share it.
- The Anthropic provider does not forward `temperature`: the current Anthropic SDK no longer accepts it.
- No metrics, tracing or structured logs yet.
- The model registry is static and may be out of date.
- Several top-level package names (`api`, `utils`, `providers`, ...) are generic. A `src/` layout is planned.
- Security: run behind TLS. See `SECURITY.md`.

## Where this is going

Planned, not built: hardening the gateway (failover tests, timeouts, metrics, budgets), an OpenAI-compatible API, a tools provider over MCP, a rebuilt agent harness, and local GGUF model support (LM Studio and Ollama first, then a managed llama.cpp runtime). The full plan is in [docs/roadmap.md](docs/roadmap.md); progress is tracked in the issue tracker.

## License

GPL-3.0. See [LICENSE](LICENSE).
