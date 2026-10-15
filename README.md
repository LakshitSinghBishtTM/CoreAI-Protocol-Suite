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
- **Routing strategies:** `cheapest`, `fastest`, `balanced` (weighted cost and latency), `round_robin` and `fallback` (first provider, falling back on error).
- **Response caching:** in-memory or Redis.
- **Rate limiting:** per provider and global, plus a sliding-window limiter on the API keyed by API key or client IP.
- **Retries:** exponential backoff for retryable provider errors.
- **Auth:** JWT bearer tokens or `X-API-Key`, with per-endpoint scopes.
- **Streaming completions** and a **background task queue**.
- **Model registry:** capability, context window and price data per model.
- **Fine-tuning job API** (OpenAI only).
- **Storage:** SQLite by default, optional Postgres.

## Status by component

| Component | Status | Notes |
| --- | --- | --- |
| Router, providers, cache, limiter, retry | Alpha | Unit tested with mocked providers. Failover against real provider failures is not yet tested. |
| API server and auth | Alpha | Roughly half of the route code is covered by tests. |
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
| `GET /v1/providers` | Provider availability and latency `[completions:read]` |
| `GET /v1/models` | Model registry `[completions:read]` |
| `POST /v1/tasks`, `GET` and `DELETE /v1/tasks/{id}` | Background tasks `[tasks:read, tasks:write]` |
| `POST /v1/agents`, `GET /v1/agents`, `DELETE /v1/agents/{id}` | Experimental agents `[agents:read, agents:write]` |
| `GET /v1/stats` | Runtime statistics `[admin]` |
| `POST /v1/admin/shutdown` | Shutdown `[admin]` |
| `POST`, `GET` and `DELETE /v1/finetune/jobs` | Fine-tuning jobs `[admin]` |

Interactive docs are at `/docs` while the server runs.

## Configuration

See `.env.example` for a template. The main variables:

| Variable | Purpose |
| --- | --- |
| `SECRET_KEY` | Signs JWTs. Required. |
| `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `GROK_API_KEY`, `DEEPSEEK_API_KEY` | Enable each provider. At least one is required. |
| `DATABASE_URL` | Postgres URL. Empty means SQLite (install `.[postgres]` for Postgres). |
| `DB_PATH` | SQLite file location. |
| `JWT_EXPIRY_MINS` | Token lifetime. |
| `WEBHOOK_SIGNING_SECRET` | Verifies signed webhook requests. |

## Development

```bash
pip install -e .[dev]
pytest
flake8 coreai/ api/ agents/ && black --check coreai/ api/ agents/ && isort --check-only coreai/ api/ agents/
```

CI runs lint, tests on Python 3.11 and 3.12, a wheel build-and-import check, and a Docker build with a health check.

## Known limitations

- Provider failover and circuit breaking are not yet tested against real failures, and there are no per-provider timeouts or request deadlines.
- No metrics, tracing or structured logs yet.
- The model registry is static and may be out of date.
- Several top-level package names (`api`, `utils`, `providers`, ...) are generic. A `src/` layout is planned.
- Security: run behind TLS. See `SECURITY.md`.

## Where this is going

Planned, not built: hardening the gateway (failover tests, timeouts, metrics, budgets), an OpenAI-compatible API, a tools provider over MCP, a rebuilt agent harness, and local GGUF model support (LM Studio and Ollama first, then a managed llama.cpp runtime). The full plan is in [docs/roadmap.md](docs/roadmap.md); progress is tracked in the issue tracker.

## License

GPL-3.0. See [LICENSE](LICENSE).
