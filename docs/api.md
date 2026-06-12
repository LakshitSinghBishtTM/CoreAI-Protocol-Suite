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
| 429  | Rate limited            |
| 500  | Internal platform error |
| 503  | Service unavailable     |

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

| Header          | Purpose             |
| --------------- | ------------------- |
| X-Request-ID    | Request correlation |
| X-Response-Time | Execution latency   |

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
