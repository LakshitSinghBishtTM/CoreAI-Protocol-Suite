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

**Gateway**
- **Providers:** OpenAI, Anthropic, Gemini, Grok and DeepSeek behind one interface, plus local OpenAI-compatible servers (LM Studio, Ollama, `llama-server`, vLLM) and a managed GGUF runtime (`localrt`). See [docs/providers.md](docs/providers.md) and [docs/local-models.md](docs/local-models.md).
- **Routing:** `cheapest`, `fastest`, `balanced` (weighted cost and latency), `round_robin` and `fallback`, plus named policies in `config/routing.yml` and `POST /v1/routing/explain` to preview a decision. See [docs/routing.md](docs/routing.md).
- **Failover:** if a provider fails with overload (429, 503, 529), a 5xx, a timeout or a connection error, the request moves to the next-best provider. Requests that pin a provider or a specific model do not fail over. Streams fail over only before the first chunk is sent.
- **Circuit breakers, timeouts, deadlines and retries:** a failing provider is skipped for a cooldown, then probed with one request; each attempt has a timeout and one deadline covers all retries and failovers; retries use exponential backoff with jitter, honor `Retry-After`, and never retry bad requests or auth errors.
- **Response caching:** in-memory (LRU, size-limited) or Redis.
- **Rate limiting:** per provider (requests, tokens, concurrency), global, and a sliding-window limiter on the API keyed by API key or client IP.
- **Budgets and usage:** per-key and per-model caps from `config/budgets.yml` ([docs/budgets.md](docs/budgets.md)), and `GET /v1/usage` and `/v1/usage/report`. The usage ledger is in memory and resets on restart.
- **Streaming** completions with token and cost metering.

**APIs**
- Native `/v1/completions`, OpenAI-compatible `/v1/chat/completions`, `/v1/embeddings` and `/v1/models`, and Anthropic-compatible `/v1/messages`. See [docs/api.md](docs/api.md).
- Auth with JWT bearer tokens or `X-API-Key`, with per-endpoint scopes.
- Background task queue, fine-tuning job API (OpenAI only), audit log, and signed webhooks and Slack/email alerts ([docs/events.md](docs/events.md)).

**Tools, agents and data** (opt-in, enabled by config files)
- **Tools provider:** built-in tools, an MCP endpoint (JSON-RPC 2.0 at `POST /mcp`), an MCP client, sandboxing and human approvals ([docs/tools.md](docs/tools.md)).
- **Agent harness:** a tool-use loop with step, cost, token and deadline limits, checkpoints, pause/resume/cancel and a kill switch, exposed as `/v1/runs` ([docs/agents.md](docs/agents.md)).
- **Memory, RAG and feedback** stores with their own APIs, and guardrails for PII and prompt injection (`config/guardrails.example.yml`).

**Operations**
- Prometheus metrics at `/metrics` (admin token), JSON logs with request IDs, optional OpenTelemetry tracing, and `/healthz` and `/readyz` probes ([docs/operations.md](docs/operations.md)).
- Dockerfile, Docker Compose, Kubernetes manifests, a Helm chart and Grafana/Prometheus assets under `deploy/` ([docs/deployment.md](docs/deployment.md)).
- A `caips` command line ([docs/cli.md](docs/cli.md)), a Python SDK (`src/caips_sdk`) and a TypeScript SDK (`sdk/typescript`), and an eval runner ([docs/evals.md](docs/evals.md)).
- Storage: SQLite by default, optional Postgres.

## Status by component

| Component | Status | Notes |
| --- | --- | --- |
| Router, failover, circuit breaker, retry, limiter | Alpha | Tested with simulated failures, and with the real Anthropic, OpenAI and Gemini SDK error types over mock HTTP transports. Not yet exercised against a live provider outage. |
| Providers and cache | Alpha | Anthropic and OpenAI have request/response contract tests; Gemini, Grok and DeepSeek do not yet. |
| API server and auth | Alpha | Error mapping, streaming and provider health are tested; roughly two thirds of the route code is covered. |
| Task orchestrator | Alpha | Queue and dispatch exist; limited tests. |
| Model registry (`neural/model.py`) and pricing (`config/pricing.yml`) | Alpha | Static tables; prices are marked unreviewed and go stale. |
| Fine-tuning API (`neural/training.py`) | Optional | OpenAI only; Anthropic is a stub. |
| Agent harness (`harness/`, `/v1/runs`) | Alpha | Tool-use loop, checkpoints, approvals. Opt-in via `config/agents.yml`. Not yet: agent memory, sub-agents, scheduled runs. |
| Tools and MCP (`tools/`) | Alpha | Built-in tools, MCP server and client, approvals. Opt-in via `config/tools.yml`. |
| Local models (`localrt/`, `providers/local.py`) | Alpha | Connect mode and a managed GGUF runtime. Tested with fakes, not across hardware. |
| Memory, RAG, feedback, guardrails | Alpha | Each has an API and tests; retrieval quality is not benchmarked. |
| Legacy agents (`agents/`) | Experimental | Lifecycle management only; no tools. Superseded by the harness. See `src/agents/README.md`. |
| Distributed and secure protocols (`protocols/`) | Experimental | Not used by the request path. The custom crypto is **unaudited**; do not rely on it. See `src/protocols/README.md`. |
| Legacy cost helpers (`utils/cost.py`) | Not wired in | A facade over `config/pricing.yml`; the router prices through `coreai/pricing.py`. |
| Request logging and validation (`middleware/`) | Not wired in | The server uses `src/api/middleware.py` and `coreai/request_context.py`. |
| SQL migrations (`migrations/`, `src/database/migrations.py`) | Not wired in | Not run at startup. |

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

The code lives in `src/` (the install exposes `api`, `coreai`, `providers`, ... as top-level packages). The wheel contains code only: `config/` and `migrations/` are read from the checkout, or from `$COREAI_HOME`.

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

The `caips` CLI and the SDKs default to `http://localhost:8000`, not the port above. Point them at the server with `CAIPS_URL` (or `--url`):

```bash
export CAIPS_URL=http://localhost:8743 CAIPS_API_KEY="$TOKEN"
caips health
```

### Docker

```bash
docker build -t caips .
docker run -p 8743:8743 --env-file .env caips
# or, with SQLite on a named volume:
cp .env.example .env && docker compose up
```

Kubernetes manifests and a Helm chart are in `deploy/`; see [docs/deployment.md](docs/deployment.md).

## API

All routes except the health probes need authentication. The scope a route needs is in brackets.

| Method and path | Purpose |
| --- | --- |
| `GET /health`, `/healthz`, `/readyz` | Liveness, loaded providers, readiness (no auth) |
| `GET /metrics` | Prometheus metrics `[admin]` |
| `POST /v1/completions` | Completion through the router `[completions:write]` |
| `POST /v1/completions/stream` | Streaming completion `[completions:write]` |
| `POST /v1/routing/explain` | Show what a request would do without sending it |
| `POST /v1/chat/completions` | OpenAI-compatible chat, with SSE streaming `[completions:write]` |
| `POST /v1/embeddings` | OpenAI-compatible embeddings |
| `POST /v1/messages` | Anthropic-compatible messages |
| `GET /v1/models` | OpenAI-compatible model list `[completions:read]` |
| `GET /v1/models/details` | Model registry with prices and capabilities `[completions:read]` |
| `GET /v1/providers` | Provider health from circuit breakers and recent errors (no network calls) `[completions:read]` |
| `GET /v1/usage`, `/v1/usage/report` | Usage and cost over time, as JSON or CSV |
| `POST /v1/tasks`, `GET` and `DELETE /v1/tasks/{id}` | Background tasks `[tasks:read, tasks:write]` |
| `/v1/runs` (start, list, status, events, stream, cancel, pause, resume) | Agent harness runs; opt-in |
| `GET /v1/tools`, `POST /mcp`, `/v1/tools/approvals` | Tools, MCP endpoint, tool-call approvals; opt-in |
| `/v1/local/models` | List, pull, remove and unload local GGUF models |
| `/v1/memory`, `/v1/rag/{collection}`, `/v1/feedback` | Memory, retrieval and feedback stores |
| `POST /v1/agents`, `GET /v1/agents`, `DELETE /v1/agents/{id}` | Legacy experimental agents `[agents:read, agents:write]` |
| `GET /v1/stats`, `GET /v1/admin/audit`, `POST /v1/admin/shutdown` | Statistics, audit log, shutdown `[admin]` |
| `/v1/finetune/estimate`, `/v1/finetune/jobs` | Fine-tuning cost estimate and jobs `[admin]` |

Scopes and request bodies for each route are in [docs/api.md](docs/api.md).

Interactive docs are at `/docs` while the server runs. For a terminal UI over the same API, see [docs/tui.md](docs/tui.md).

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
| `COREAI_HOME` | Directory holding `config/` and `migrations/` when running outside the checkout. |
| `COREAI_ROUTING_POLICY_FILE`, `COREAI_BUDGET_FILE`, `COREAI_PRICING_FILE` | Override `config/routing.yml`, `config/budgets.yml` and `config/pricing.yml`. |
| `COREAI_AGENTS_FILE`, `COREAI_TOOLS_FILE`, `COREAI_LOCAL_FILE`, `COREAI_GUARDRAILS_CONFIG` | Enable and configure the harness, tools, local models and guardrails. |
| `COREAI_LOG_FORMAT`, `COREAI_LOG_LEVEL`, `COREAI_LOG_FILE` | Log format (`json` default or `text`), level and optional rotating file. |
| `COREAI_TRACING`, `OTEL_EXPORTER_OTLP_ENDPOINT` | Turn on OpenTelemetry tracing (`pip install 'coreai-protocol-suite[tracing]'`). |
| `CAIPS_URL`, `CAIPS_API_KEY` | Used by the `caips` CLI and evals, not by the server. |

More variables (alerts, SMTP, plugins, storage paths) are documented in [docs/operations.md](docs/operations.md), [docs/events.md](docs/events.md) and [docs/deployment.md](docs/deployment.md).

## Development

```bash
pip install -e .[dev]
pytest
ruff check . && black --check .
mypy                      # checks the files listed in pyproject.toml
```

CI runs lint, tests on Python 3.11 and 3.12, a wheel build-and-import check, and a Docker build with a health check. See [CONTRIBUTING.md](CONTRIBUTING.md) and [docs/development.md](docs/development.md).

## Known limitations

- Failover is verified with simulated failures, not yet against a real provider outage. Circuit breaker state is per process, so replicas do not share it.
- The Anthropic provider does not forward `temperature`: the current Anthropic SDK no longer accepts it.
- The usage ledger is in memory and resets on restart.
- The model registry is static and may be out of date.
- Several top-level package names (`api`, `utils`, `providers`, ...) are generic and installed side by side in `site-packages`. Renaming them under one namespace is not done.
- Security: run behind TLS. See `SECURITY.md`.

## Documentation

Start with [docs/concepts.md](docs/concepts.md) and [docs/architecture.md](docs/architecture.md). Other guides: [API](docs/api.md), [providers](docs/providers.md), [routing](docs/routing.md), [budgets](docs/budgets.md), [tools](docs/tools.md), [agents](docs/agents.md), [local models](docs/local-models.md), [operations](docs/operations.md), [deployment](docs/deployment.md), [security](docs/security.md), [CLI](docs/cli.md), [evals](docs/evals.md), [plugins](docs/plugins.md), [events](docs/events.md) and the [glossary](docs/glossary.md). Release notes are in [CHANGELOG.md](CHANGELOG.md).

## Where this is going

The plan is in [docs/roadmap.md](docs/roadmap.md); progress is tracked in the issue tracker. Gateway hardening comes first (live-outage testing, persistent usage), then agent memory and sub-agents, and a web UI.

## License

GPL-3.0. See [LICENSE](LICENSE).
