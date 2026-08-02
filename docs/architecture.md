# Architecture

## Overview

CoreAI Protocol Suite is a protocol-oriented AI infrastructure platform for coordinating language model providers and autonomous agents behind explicit, versioned wire protocols rather than ad hoc function calls between components.

The platform provides a unified control plane for routing inference requests, orchestrating agent workflows, enforcing operational policies, and managing execution lifecycles.

CoreAI is designed around modular subsystems that can operate independently or as part of a larger deployment. The architecture separates provider integrations, communication protocols, orchestration services, and persistence layers into distinct operational domains. The protocol layer in particular (DAP, STP) is built with multi-node communication in mind - message envelopes, delivery semantics, and authenticated transport - but today CoreAI runs as a single process; see "Deployment Model" below for what's actually supported now versus planned.

---

# Architectural Goals

CoreAI is built around five primary design objectives:

### Provider Independence

Applications should not depend directly on a specific model vendor.

Provider implementations are abstracted behind a common interface, allowing workloads to be routed across OpenAI, Anthropic, Gemini, Grok, DeepSeek, and future providers without application-level changes.

### Protocol-Driven Coordination

Communication between system components is governed by explicit protocols rather than implementation-specific interfaces.

Protocols define message formats, delivery guarantees, transport security requirements, and coordination semantics.

### Distributed Execution (planned, not yet built)

Execution workloads should eventually be capable of scaling beyond a single process or host.

The protocol layer is designed for this from the start - DAP's message envelopes and STP's session/authentication model don't assume a single process. But there is currently no worker registry, cluster coordinator, or network transport connecting separate CoreAI instances; an earlier `runtime/` package that attempted this was removed as dead code (zero callers, zero test coverage) rather than left half-built. Today, DAP and STP both run within one process. Treat this goal as a target the protocol layer is built to grow into, not a description of current capability.

### Operational Visibility

Every significant action within the platform should be observable.

Request execution, provider usage, task lifecycles, routing decisions, costs, latency metrics, and infrastructure health are exposed through telemetry and persistence layers.

### Fault-Tolerant Operation

Failures are expected.

Routing, task execution, distributed coordination, and protocol processing are designed to continue operating under partial system failure conditions.

---

# High-Level Architecture

```
┌────────────────────────────────────────────────────┐
│                    Client Systems                  │
└──────────────────────────┬─────────────────────────┘
                           │
                           ▼
┌────────────────────────────────────────────────────┐
│                      API Layer                     │
│ Authentication • Validation • Routing Endpoints    │
└──────────────────────────┬─────────────────────────┘
                           │
                           ▼
┌────────────────────────────────────────────────────┐
│                Orchestration Layer                 │
│ Router • Orchestrator • Scheduler                  │
└──────────────┬───────────────────┬─────────────────┘
               │                   │
               ▼                   ▼
┌──────────────────────┐   ┌────────────────────────┐
│   Provider Layer     │   │      Agent Layer       │
│                      │   │                        │
│ OpenAI               │   │ Agent Manager          │
│ Anthropic            │   │ Autonomous Agents      │
│ Gemini               │   │ Task Orchestrator      │
│ Grok                 │   │ Memory Management      │
│ DeepSeek             │   │                        │
└──────────┬───────────┘   └───────────┬────────────┘
           │                           │
           └───────────────┬───────────┘
                           ▼
┌────────────────────────────────────────────────────┐
│                  Protocol Layer                    │
│ DAP (agent messaging) • STP (message auth)         │
└──────────────────────────┬─────────────────────────┘
                           │
                           ▼
┌────────────────────────────────────────────────────┐
│                 Persistence Layer                  │
│ Requests • Tasks • Agents • Usage Data             │
└────────────────────────────────────────────────────┘
```

---

# Core Subsystems

## API Layer

The API layer exposes CoreAI functionality through HTTP interfaces and serves as the primary ingress point for client applications.

Responsibilities include:

* Request authentication
* Authorization enforcement
* Payload validation
* Request normalization
* Completion APIs
* Task submission APIs
* Operational endpoints

The API layer does not perform inference directly. Instead, requests are delegated to orchestration components that determine execution strategies.

---

## Orchestration Layer

The orchestration layer coordinates decision-making across the platform.

It is responsible for:

* Provider selection
* Routing policy enforcement
* Agent assignment
* Task scheduling
* Resource coordination
* Workflow execution

The orchestration layer acts as the control plane of the platform.

### Router

The router determines how inference requests are executed.

Routing decisions may consider:

* Provider availability
* Cost characteristics
* Request requirements
* Operational policies
* Runtime constraints

### Orchestrator

The orchestrator coordinates agent execution and long-running task lifecycles.

Responsibilities include:

* Task assignment
* Task tracking
* Agent coordination
* Execution monitoring

### Scheduler

The scheduler manages recurring system operations and maintenance workflows.

Examples include:

* Health checks
* Cache maintenance
* Statistics collection
* Background maintenance jobs

---

## Provider Layer

The provider layer abstracts external AI services behind a unified interface.

Provider implementations expose common capabilities including:

* Completion generation
* Streaming responses
* Token estimation
* Cost estimation
* Capability reporting

Current integrations include:

* OpenAI
* Anthropic
* Gemini
* Grok
* DeepSeek

New providers can be added without modifying orchestration logic.

---

## Agent Layer

The agent layer provides persistent execution environments for autonomous workflows.

Agents maintain state, execute objectives, coordinate tasks, and interact with provider infrastructure through the routing layer.

### Agent Manager

Maintains the lifecycle of registered agents.

Responsibilities include:

* Registration
* Discovery
* Status management
* Capability tracking

### Autonomous Agents

Autonomous execution units capable of:

* Multi-step reasoning
* Iterative execution
* Task progression
* Context management

### Task Orchestrator

Coordinates execution across multiple agents and tracks task state throughout its lifecycle.

---

## Protocol Layer

Protocols define how components communicate throughout the platform.

CoreAI treats protocols as first-class architectural components rather than implementation details.

### Distributed Agent Protocol (DAP)

Defines coordination semantics between agents.

Responsibilities include:

* Message delivery
* Capability advertisement
* Agent discovery
* Acknowledgements
* Routing metadata

Every message is wrapped in a `MessageEnvelope` and sealed through STP (below) before delivery, and verified before an agent can read it.

### Secure Transport Protocol (STP)

Provides authenticated, replay-protected message sealing. `AgentMessageRouter` holds one STP session per registered agent and uses it to seal every `MessageEnvelope` before queuing and verify it on delivery - real HMAC-SHA256 integrity and a replay window on every DAP message, not just the unauthenticated checksum `MessageEnvelope` computes on its own.

Capabilities include:

* Session establishment
* Frame encoding
* Message authentication (HMAC-SHA256)
* Replay protection (sliding time window)
* Session key management

Sender and verifier both run in the same process today - DAP has no cross-process transport yet (see Distributed Execution, above) - so this is forward-compatible hardening for the day DAP is backed by a real broker, not a live cross-node trust boundary yet.

---

## Persistence Layer

The persistence layer stores operational and execution data.

### Request Logs

Records inference activity including:

* Provider usage
* Token consumption
* Cost metrics
* Latency metrics
* Cache information

### Agent Registry

Stores agent metadata and lifecycle information.

### Task Records

Persists task execution state and results.

### Usage Analytics

Maintains aggregated operational metrics used for reporting and observability.

---

# Request Lifecycle

A typical completion request follows the sequence below:

```
Client Request
      │
      ▼
API Validation
      │
      ▼
Authentication
      │
      ▼
Router Selection
      │
      ▼
Provider Resolution
      │
      ▼
Provider Execution
      │
      ▼
Cost & Token Accounting
      │
      ▼
Persistence & Logging
      │
      ▼
Response Delivery
```

---

# Distributed Task Lifecycle

Agent workloads follow a separate execution path.

```
Task Submission
       │
       ▼
Orchestrator
       │
       ▼
Agent Assignment
       │
       ▼
Agent Message Delivery (DAP/STP)
       │
       ▼
Task Completion
       │
       ▼
Persistence
```

---

# Deployment Model

CoreAI supports multiple deployment topologies.

### Standalone

Single-node deployment suitable for development and evaluation.

### Service Deployment

Dedicated API and orchestration services operating on shared infrastructure.

### Distributed Cluster (not currently supported)

Multi-node deployment with coordinated execution, worker registration, and distributed task processing is the direction the protocol layer (DAP/STP) is designed to grow into, but there is no worker registry, cluster coordinator, or inter-process transport in the codebase today - see "Distributed Execution" under Architectural Goals, above. Don't deploy CoreAI expecting this topology to work yet.

---

# Design Philosophy

CoreAI is designed as infrastructure rather than application software.

The platform does not prescribe how intelligence is implemented, which providers are used, or how agents reason.

Instead, CoreAI provides the coordination, communication, execution, and operational mechanisms required to build reliable AI systems at scale.
