# CaiPS Roadmap: Remaining Work

This file lists only what is still open. Finished work (gateway reliability, routing policies, budgets, OpenAI and Anthropic compatible APIs, tools layer and MCP, agent harness, GGUF runtime, memory, RAG, guardrails, evals, fine-tuning, CLI, TUI, Helm and k8s manifests) has been removed; see `CHANGELOG.md` and the per-layer docs for it.

Principles still apply: layers depend downward only, every layer ships alone, claims match tests, and numbers come from scripts in the repo.

**Non-goals:** training models from scratch, a hosted SaaS offering, multi-agent swarms.

## 1. Gateway reliability and verification

- Exercise failover, breakers and retries against a real provider outage (staged outage or a scheduled live job with tiny requests), not only mocks and simulated failures.
- Share circuit breaker state across replicas (it is per process today).
- Raise API route coverage (about two thirds) and add tests for the task orchestrator.
- Publish chaos-test results and p50/p99 gateway overhead from `scripts/chaos.py` and `scripts/loadtest.py`; target under 20 ms p99.
- Run a memory profile (`tracemalloc` or `memray`) under load and confirm every queue and cache is bounded.
- Verify cost accuracy against provider invoices (target within 1%) and refresh the static model registry and price tables so they don't go stale.
- Forward `temperature` correctly for Anthropic models once the SDK allows it.
- Make runtime budget and policy edits persistent (they reset on restart).
- Add a token-issuing endpoint (tokens are minted by hand today).

## 2. Storage and packaging

- Confirm PyPI trusted publishing end to end with a real release: the workflow, `twine check` and provenance defaults are tested, but it needs the publisher configured on pypi.org and a tagged release.
- Run the Postgres tests (migrations, run store, `db_query`, backups) in CI against `postgres:16`; they pass locally on a real server, and the CI job now installs `pg_dump`.

## 3. Tools layer

- Container or microVM code-execution runner (the current sandbox is process level).
- Streamed tool use for the Anthropic-compatible `/v1/messages` endpoint.
- Tool calling and structured output for local models.

## 5. Local models

- Pin a tested `llama-server` release for `python -m localrt install-runtime`, and add scheduled runtime updates.
- Expose the GGUF model registry fully through API and CLI (listing, inspect and pull exist; the registry itself is open per the old roadmap).
- Run model pulls as background jobs instead of inline.
- Add presets for LM Studio, Ollama, `llama-server` and vLLM with model auto-discovery through `/v1/models`.
- Optional speculative decoding with a draft model, and prompt-cache reuse.
- Cold-load first-token benchmark per model size.
- Use chat templates from the GGUF through llama.cpp for every managed model.

## 6. Memory, RAG and caching

- Vector backend for long-term memory.
- SQLite-vec or pgvector backend for RAG (brute-force cosine today).
- Test PDF ingestion (needs optional `pypdf`; untested).
- Optional semantic response cache with per-tenant isolation and privacy notes.
- Automatic difficulty classifier for local-versus-cloud routing (today it is a caller-set hint).

## 7. Evals and fine-tuning

- Agent replay in the eval suite.
- CLI rubric grading.
- Fine-tuning: stream job logs, set per-model budget defaults for fine-tuned models, verify price tables (marked UNVERIFIED), and add a dataset-upload API.
- Anthropic fine-tuning is a stub; support it only after the provider API is verified. Later: other providers' fine-tuning and LoRA adapters for local GGUF models.

## 8. Multi-tenancy and access

Not started. Single-user mode stays the default.

- Organizations, teams and users with roles (admin, developer, viewer) and per-tenant data isolation.
- SSO through OIDC and SAML.
- Scoped API keys with rotation and expiry.
- Per-tenant budgets, rate limits, tool allow lists and log retention.

## 9. SDKs, plugins and integrations

- Typed agent and tool clients in the Python and TypeScript SDKs (the TypeScript SDK exists under `sdk/typescript`; check how complete it is).
- Stable plugin API for providers, tools, guardrails and routing strategies, plus a registry of community plugins.
- OpenTelemetry tracing coverage for tool and agent steps.
- Usage reports: confirm CSV and JSON export per key and model.

## 10. Deployment

- Validate the Terraform example (`deploy/terraform/`, unvalidated).
- Zero-downtime config reload.
- Backups that copy model files (listed only today) and cover Postgres.
- Documented upgrade path between versions.
- Optional OpenAPI docs site and public status page template.
- Single-binary or single-container local mode.

## Exit criteria

- A new team can deploy CaiPS with the Helm chart, create a tenant, add providers and a local model, run an agent with approvals, and see its cost through the API or CLI.
- A chaos script can kill or slow any single provider without failed requests, and the result is published.
- The OpenAI SDK works with a base-URL change only, verified in CI.

## Success metrics

| Metric | Target | Measured by |
| --- | --- | --- |
| Runtime dependencies | Under 20 | Dependency report in CI |
| Coverage on router, retry, breaker, limiter | 85% or more | Coverage report |
| Requests failed when one provider is down | Zero | Chaos script |
| Gateway overhead added to a request | p99 published, under 20 ms | Load test script |
| Cost accuracy | Within 1% of invoices | Monthly reconciliation |
| Local model first-token time after cold load | Published per model size | Benchmark script |
| Agent run success on the eval suite | No regressions per release | Eval suite in CI |
| Agent runs exceeding their budget | Zero | Budget enforcement tests |
| README claims without a test or benchmark | Zero | Docs review checklist |

## Decisions already made

- License stays GPL-3.0.
- Fine-tuning stays an optional extra.
- SQLite by default, Postgres optional.
- Audience is both self-hosters and teams; tenants, roles and SSO are opt-in.
