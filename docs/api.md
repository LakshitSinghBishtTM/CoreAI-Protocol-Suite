# API

## Overview

The CoreAI API provides the primary interface through which clients interact with the platform.

The API exposes capabilities for:

* Model inference
* Agent execution
* Task management
* System observability
* Platform administration

The API is designed around a resource-oriented architecture and serves as the public entry point to the CoreAI control plane.

---

# Design Principles

## Consistency

All endpoints follow consistent request and response conventions.

This reduces client complexity and simplifies integration.

---

## Predictability

API behavior should be deterministic and transparent.

Requests should produce well-defined outcomes and expose meaningful error information.

---

## Observability

Every request is traceable.

The platform provides request identifiers, execution metadata, latency information, and operational context throughout the request lifecycle.

---

## Security

Authentication and authorization are enforced at the API boundary.

Requests are validated before reaching orchestration or execution systems.

---

# API Architecture

```
┌──────────────────────────────────────────────┐
│                 Client Apps                  │
└─────────────────────┬────────────────────────┘
                      │
                      ▼
┌──────────────────────────────────────────────┐
│             Authentication Layer             │
└─────────────────────┬────────────────────────┘
                      │
                      ▼
┌──────────────────────────────────────────────┐
│              Validation Layer                │
└─────────────────────┬────────────────────────┘
                      │
                      ▼
┌──────────────────────────────────────────────┐
│                 API Routes                   │
└─────────────────────┬────────────────────────┘
                      │
                      ▼
┌──────────────────────────────────────────────┐
│           Orchestration Services             │
└──────────────────────────────────────────────┘
```

---

# Route index

The default port is 6969. Routes marked "opt-in" exist only when their config file is present (see the README). The sections below describe the native API in detail; the OpenAI- and Anthropic-compatible routes are at the end.

| Area | Routes |
| --- | --- |
| Completions | `POST /v1/completions`, `POST /v1/completions/stream`, `POST /v1/routing/explain` |
| OpenAI-compatible | `POST /v1/chat/completions`, `POST /v1/embeddings`, `GET /v1/models` |
| Anthropic-compatible | `POST /v1/messages` |
| Registry and providers | `GET /v1/models/details`, `GET /v1/providers` |
| Usage | `GET /v1/usage`, `GET /v1/usage/report` |
| Tasks | `POST /v1/tasks`, `GET` and `DELETE /v1/tasks/{id}` |
| Agent runs (opt-in) | `/v1/runs`, `/v1/runs/{id}`, `/events`, `/stream`, `/cancel`, `/pause`, `/resume`, `POST /v1/runs/kill` |
| Tools (opt-in) | `GET /v1/tools`, `POST /mcp`, `/v1/tools/approvals` |
| Provider keys | `GET /v1/keys`, `GET /v1/keys/catalog`, `POST /v1/keys`, `DELETE /v1/keys/{id}` (admin; secrets never returned) |
| Local models | `GET /v1/local/models`, `.../{id}`, `POST .../pull`, `DELETE .../{id}`, `POST .../{id}/unload` |
| Memory, RAG, feedback | `/v1/memory`, `/v1/rag/{collection}/documents` and `/search`, `/v1/feedback` |
| Legacy agents | `POST` and `GET /v1/agents`, `DELETE /v1/agents/{id}` |
| Admin | `GET /v1/stats`, `GET /v1/admin/audit`, `POST /v1/admin/shutdown`, `/v1/finetune/estimate`, `/v1/finetune/jobs` |
| Operations | `GET /health`, `/healthz`, `/readyz` (no auth), `GET /metrics` (admin) |

Interactive OpenAPI docs are served at `/docs`.

---

# Base URL

Production deployments expose the API through a configurable endpoint.

Example:

```
https://api.example.com
```

All routes are relative to the configured deployment URL.

---

# Authentication

## API Keys

CoreAI uses API key authentication for client requests.

API keys represent authenticated identities within the platform.

Every request requiring authentication must include a valid credential.

---

## Authentication Header

```
X-API-Key: <api-key>
```

Requests without valid credentials are rejected before execution.

---

## Identity Propagation

Once authenticated, request identity is propagated throughout the execution lifecycle.

Identity information may be used for:

* Usage reporting
* Cost attribution
* Auditing
* Access control
* Operational analytics

---

# Request Lifecycle

Every API request follows the same high-level processing flow.

```
Client Request
       │
       ▼
Authentication
       │
       ▼
Validation
       │
       ▼
Routing
       │
       ▼
Execution
       │
       ▼
Persistence
       │
       ▼
Response
```

This lifecycle remains consistent across all endpoint categories.

---

# Content Types

## Request Format

Requests should use JSON payloads.

```
Content-Type: application/json
```

---

## Response Format

Responses are returned as JSON.

```
Content-Type: application/json
```

---

# Completion API

## Overview

The Completion API provides access to provider-routed inference capabilities.

Completion requests are automatically processed through the routing layer.

Provider selection may occur automatically or be explicitly requested.

---

## Endpoint

```
POST /v1/completions
```

---

## Request Structure

Completion requests contain:

| Field       | Description                 |
| ----------- | --------------------------- |
| messages    | Conversation payload        |
| provider    | Optional provider selection |
| max_tokens  | Generation limit            |
| temperature | Sampling configuration      |

---

## Execution Flow

```
Completion Request
         │
         ▼
Validation
         │
         ▼
Router
         │
         ▼
Provider Selection
         │
         ▼
Execution
         │
         ▼
Response
```

---

## Response Metadata

Completion responses may include:

* Generated content
* Provider information
* Model information
* Token usage
* Cost estimates
* Latency measurements
* Cache information

---

# Streaming API

## Overview

The Streaming API provides incremental response delivery.

Streaming enables clients to receive generated content as it becomes available.

---

## Endpoint

```
POST /v1/completions/stream
```

---

## Use Cases

Streaming is recommended for:

* Interactive applications
* Long-form generation
* User-facing interfaces
* Low-latency experiences

---

# Task API

## Overview

The Task API enables asynchronous execution through the agent subsystem.

Tasks are suitable for workloads that exceed the lifecycle of a single request.

---

## Submit Task

```
POST /v1/tasks
```

Creates a new task and submits it to the orchestration layer.

---

## Retrieve Task

```
GET /v1/tasks/{task_id}
```

Returns current task status and execution information.

---

# Task Lifecycle

Tasks progress through a defined lifecycle.

```
Pending
   │
   ▼
Running
   │
   ▼
Completed
```

Failure paths:

```
Running
   │
   ├──► Failed
   │
   └──► Cancelled
```

---

# Agent API

> The supported agent API is the harness run API under `/v1/runs` (see [agents.md](agents.md)). The `/v1/agents` routes are the legacy lifecycle API and are experimental.

## Overview

The Agent API provides visibility into agent activity and execution state.

Depending on deployment configuration, agent endpoints may expose:

* Agent registration
* Agent discovery
* Agent status
* Agent metrics
* Capability metadata

---

# Observability API

## Overview

Operational endpoints expose system telemetry and execution statistics.

These endpoints are intended for monitoring, reporting, and troubleshooting.

---

## Statistics Endpoint

```
GET /v1/stats
```

Provides aggregated platform metrics.

Examples include:

* Request counts
* Error rates
* Provider utilization
* Cost metrics
* Cache metrics
* Runtime information

---

## Health Endpoint

```
GET /health
```

Returns platform health information.

Health checks are intended for:

* Load balancers
* Monitoring systems
* Orchestrators
* Deployment tooling

---

# Validation

## Overview

All incoming requests undergo validation before execution.

Validation protects platform resources and prevents malformed requests from entering execution systems.

---

## Message Validation

Validation includes:

* Structure verification
* Content requirements
* Message limits
* Size constraints

---

## Parameter Validation

The API validates generation parameters such as:

* Temperature
* Token limits
* Provider selection

Invalid values result in request rejection.

---

# Request Limits

Deployments may define operational limits including:

* Maximum request size
* Maximum message count
* Token limits
* Concurrency limits
* Rate limits

Limit policies are deployment-specific.

---

# Error Model

## Overview

Errors are returned using structured responses.

Clients should not rely on error message text for application logic.

Instead, clients should use status codes and error categories.

---

## Common Status Codes

| Code | Meaning                 |
| ---- | ----------------------- |
| 200  | Success                 |
| 400  | Invalid request         |
| 401  | Authentication required |
| 403  | Access denied           |
| 404  | Resource not found      |
| 409  | Resource conflict       |
| 422  | Validation failure      |
| 429  | Rate limited, or a budget or provider limit reached |
| 500  | Internal platform error |
| 502  | Provider unreachable, returned a 5xx, or rejected the gateway's key |
| 503  | Service unavailable: provider overloaded, every provider failed or is circuit-broken, or the server is draining |
| 504  | Provider timed out or the request deadline was exceeded |

---

# Rate Limiting

Deployments may enforce request limits to ensure platform stability.

Rate limiting may be applied based on:

* API keys
* Users
* Organizations
* Providers
* Deployment policies

Exceeded limits result in request rejection until quota becomes available.

---

# Request Tracing

Every request receives a unique identifier.

Request identifiers support:

* Operational debugging
* Audit trails
* Distributed tracing
* Incident investigation

Clients should preserve request identifiers when communicating with platform operators.

---

# Response Headers

CoreAI may include operational metadata within response headers.

Examples include:

| Header                | Purpose                                                    |
| --------------------- | ---------------------------------------------------------- |
| X-Request-ID          | Request correlation (a safe client-supplied value is kept) |
| X-CaiPS-Provider      | Provider that served the request                           |
| X-CaiPS-Cost-USD      | Cost of a non-streaming response                           |
| X-RateLimit-Limit     | Requests allowed per minute                                |
| X-RateLimit-Remaining | Requests left this minute                                  |
| Retry-After           | Set on 429 and 503 when the wait is known                  |

These headers support monitoring and troubleshooting workflows.

---

# Versioning

The API supports explicit versioning.

Version identifiers are incorporated into route namespaces.

Example:

```
/v1/completions
```

Future platform versions may introduce additional namespaces while preserving compatibility for existing clients.

---

# Operational Characteristics

The API is designed for:

* High availability
* Horizontal scalability
* Provider independence
* Distributed execution
* Observability

The API itself contains minimal business logic and delegates execution responsibilities to orchestration, runtime, and provider subsystems.

---

# Design Philosophy

The API serves as a stable contract between clients and the CoreAI platform.

Clients should interact with platform capabilities rather than individual providers, runtimes, or agents.

This abstraction allows CoreAI deployments to evolve internally while preserving a consistent external interface for applications, services, and operational tooling.

## OpenAI-compatible API

`POST /v1/chat/completions` and `GET /v1/models` follow the OpenAI shapes, so
an OpenAI SDK works by changing the base URL and passing a CaiPS token as the
API key:

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:6969/v1", api_key="<CaiPS token>")
client.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": "hi"}])
```

The test suite runs the official OpenAI Python SDK against the gateway
(`tests/test_openai_compat.py`), including streaming.

| Request field | Behaviour |
| --- | --- |
| `model`, `messages`, `temperature`, `stream`, `stream_options.include_usage` | Supported |
| `max_tokens`, `max_completion_tokens` | Supported (the latter wins) |
| `system` and `developer` messages | Joined into the system prompt |
| Text content parts | Supported |
| `image_url` parts on user messages | Supported (see below) |
| `top_p`, `seed`, `user`, `presence_penalty`, `frequency_penalty`, `logit_bias` | Accepted and ignored |
| `tools`, `tool_choice`, assistant `tool_calls`, `tool` messages | Supported without streaming (see below) |
| `response_format` `json_object` or `json_schema` | Supported (see below) |
| `functions`, `function_call`, `stop`, `logprobs`, `n` above 1, `response_format` types other than text/json, tools with `stream: true`, audio parts | `400` with `code: "unsupported_parameter"` and the `param` name. Refused so a change in meaning is never silent |
| `provider` (extension) | Pin a provider |

- **Errors** use OpenAI's envelope `{"error": {"message", "type", "param", "code"}}`.
  Budget refusals are `429` with `Retry-After`.
- **Streaming:** if no output has started, a provider failure is a normal HTTP
  error (after failover). If it fails after output has started, the stream
  sends an error event (`code: "stream_<kind>"`) and `[DONE]` and never a
  `finish_reason`, so a client can tell the answer is incomplete.
- `finish_reason` is `"stop"`, `"length"`, `"content_filter"` or `"tool_calls"`.
- **Tool calling:** send OpenAI-style `tools` and `tool_choice`; the reply has
  `message.tool_calls` and `finish_reason: "tool_calls"`. The gateway
  translates to each provider's format (Anthropic `tool_use`/`tool_result`,
  Gemini `function_call`/`function_response`) but never runs a tool. Send the
  results back as `role: "tool"` messages with `tool_call_id`. Requests with
  tools are never cached, only go to providers and models that support tools
  (`supports_function_calling` in `/v1/models/details`), and are refused with
  `stream: true` for now. Gemini call ids are generated (`call_0`, `call_1`).
- **Images:** `image_url` parts on user messages, as `https://` links or
  `data:image/{jpeg,png,gif,webp};base64,...` URLs (20 MB of inline data per
  request). Only vision-capable providers and models are used; others are
  skipped, or the request is a 400 if the model is pinned. Gemini takes inline
  data only, so a link to Gemini is a 400. Image requests are never cached.
- **Structured output:** `json_object` and `json_schema` are native on OpenAI
  and Grok and on Gemini (JSON mime type). DeepSeek gets JSON mode with the
  schema in the prompt. Anthropic has no JSON mode, so the instruction and
  schema go in the system prompt and code fences are stripped. The schema is
  not validated by the gateway on any provider, so check the output.
- Response headers `X-CaiPS-Provider` and `X-CaiPS-Cost-USD` (non-streaming).
- Chunks carry the requested model name (`auto` if none was given).
- `GET /v1/models` lists the registry models of active providers plus each
  provider's default model. `GET /v1/models/details` is the richer registry
  that `GET /v1/models` used to return (**breaking change**).

## Anthropic-compatible API

`POST /v1/messages` follows Anthropic's Messages API, so an Anthropic SDK
works by changing the base URL (`base_url="http://localhost:6969"`) and
passing a CaiPS token as `auth_token` (or an API key as `x-api-key`). The
official SDK is exercised against it in `tests/test_vision_and_messages.py`,
including streaming.

| Request field | Behaviour |
| --- | --- |
| `model`, `max_tokens` (required), `messages`, `system`, `temperature`, `stream` | Supported. `system` may be a string or text blocks |
| `text`, `image` (base64 or URL), `tool_use`, `tool_result` blocks | Supported |
| `tools` (with `input_schema`), `tool_choice` `auto`/`any`/`none`/`tool` | Supported without streaming |
| `top_p`, `top_k`, `metadata` | Accepted and ignored |
| `stop_sequences`, documents, thinking and server-tool blocks, tools with `stream: true` | `400` |
| `provider` (extension) | Pin a provider |

- Requests go through the same router, budgets and failover as
  `/v1/chat/completions`, and may be served by any provider.
- Errors use Anthropic's envelope `{"type": "error", "error": {"type", "message"}}`.
- Streams send `message_start`, one text content block, `message_delta` and
  `message_stop`. A failure after output has started sends an `error` event and
  no `message_stop`.
- `stop_reason` is `end_turn`, `max_tokens`, `tool_use` or `refusal`.
- Token counting (`/v1/messages/count_tokens`) and the batch API are not implemented.


# Client SDKs

Both clients cover chat, tools (MCP), agent runs and approvals, and return typed responses.

**Python** (`src/caips_sdk`): `CaipsClient(base_url, api_key)`; response shapes are `TypedDict`s in `caips_sdk.types` (`Run`, `RunEvent`, `Agent`, `Tool`, `ToolResult`, `Approval`, ...).

```python
from caips_sdk import CaipsClient
with CaipsClient("http://localhost:6969", api_key=key) as c:
    run = c.run_agent("helper", "What is 17 + 25?")   # start and wait
    print(run["status"], run["result"], run["cost_usd"])
    for e in c.run_events(run["id"]): print(e["type"])
```

**TypeScript** (`sdk/typescript`, `@caips/client`): the same methods in camelCase (`runAgent`, `runEvents`, `listAgents`, ...) with exported interfaces. `npm test` compiles in strict mode and runs the tests.

Run methods: `list_agents`, `list_runs`, `start_run`, `get_run`, `run_events`, `wait_run`, `run_agent`, `pause_run`, `resume_run`, `cancel_run`, `kill_runs` (admin). `wait_run` waits until the run ends; with `stop_when_blocked=True` it also returns when the run is paused or waiting for an approval (answer it with `resume_run(id, approve=...)`, admin scope). Tool methods: `list_tools`, `call_tool` (a failing tool returns `isError: true`), `pending_approvals`, `decide`. A test compares the Python `Run` and `Approval` types with the store schema and the API so they cannot drift silently.
