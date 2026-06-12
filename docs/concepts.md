# Concepts

## Overview

CoreAI Protocol Suite is composed of several independent subsystems that work together to provide routing, orchestration, execution, communication, and observability for AI workloads.

Understanding these concepts is more important than understanding individual modules or implementation details.

This document introduces the conceptual model used throughout the platform.

---

# Platform

A Platform is a complete CoreAI deployment.

A platform consists of:

* API services
* Orchestration services
* Runtime infrastructure
* Protocol implementations
* Provider integrations
* Persistence services

The platform is the highest-level operational unit.

---

# Control Plane

The Control Plane is responsible for making decisions.

Examples include:

* Routing requests
* Assigning tasks
* Selecting providers
* Scheduling work
* Coordinating agents

Within CoreAI, the control plane is primarily implemented by:

* Router
* Orchestrator
* Scheduler

The control plane determines what should happen.

---

# Execution Plane

The Execution Plane is responsible for performing work.

Examples include:

* Running inference
* Processing tasks
* Executing workflows
* Coordinating workers

Within CoreAI, the execution plane consists primarily of:

* Runtime Engine
* Distributed Runtime
* Worker Nodes
* Agents

The execution plane determines how work happens.

---

# Provider

A Provider is an external AI service integrated into the platform.

Examples include:

* OpenAI
* Anthropic
* Gemini
* Grok
* DeepSeek

Providers execute model workloads but are not responsible for orchestration, routing, or task management.

Providers are infrastructure resources rather than platform components.

---

# Router

The Router determines which provider should execute a request.

Routing decisions may consider:

* Cost
* Availability
* Latency
* Policy requirements
* Request constraints

The Router is the primary decision engine for inference workloads.

---

# Agent

An Agent is a persistent execution entity capable of pursuing objectives.

Agents differ from requests.

A request is short-lived.

An agent is long-lived.

Agents may:

* Maintain context
* Execute tasks
* Coordinate workflows
* Interact with providers
* Participate in distributed systems

Agents are execution primitives.

---

# Task

A Task is a unit of work assigned to an agent.

Tasks possess:

* Objectives
* State
* Ownership
* Progress information

Tasks are managed by orchestration systems and executed by agents.

---

# Objective

An Objective defines a desired outcome.

Examples:

* Analyze a dataset
* Generate a report
* Coordinate a workflow
* Execute a research task

Objectives describe what should be achieved rather than how it should be achieved.

---

# Runtime

The Runtime is the execution infrastructure responsible for processing workloads.

The runtime provides:

* Execution pipelines
* Resource management
* Worker coordination
* Distributed execution

The runtime executes work assigned by orchestration systems.

---

# Worker

A Worker is a runtime node capable of executing workloads.

Workers may:

* Execute tasks
* Process requests
* Participate in distributed execution

Workers register with runtime coordinators and receive assignments dynamically.

---

# Coordinator

A Coordinator manages distributed execution.

Responsibilities include:

* Worker registration
* Health monitoring
* Task dispatch
* Load balancing

The coordinator maintains awareness of cluster state.

---

# Protocol

A Protocol defines communication behavior between components.

Protocols establish:

* Message formats
* Delivery semantics
* Security requirements
* Versioning rules

Protocols are architectural contracts rather than implementation details.

---

# Authentication Protocol

The Authentication Protocol establishes identity and access rights.

It is responsible for:

* Credential verification
* Identity establishment
* Authorization support

---

# Distributed Agent Protocol (DAP)

DAP governs communication between agents and distributed execution components.

DAP defines:

* Message envelopes
* Delivery guarantees
* Capability advertisements
* Acknowledgements

DAP is the primary coordination protocol within CoreAI.

---

# Secure Transport Protocol (STP)

STP protects communication between components.

Responsibilities include:

* Integrity verification
* Replay protection
* Session management
* Secure framing

STP provides transport-level security services.

---

# Memory

Memory represents retained execution context associated with an agent.

Memory may include:

* Message history
* Context state
* Operational metadata

Memory enables continuity across execution cycles.

---

# Capability

A Capability describes a function that an agent can perform.

Examples include:

* Analysis
* Summarization
* Research
* Coordination

Capabilities enable intelligent task assignment.

---

# Persistence Layer

The Persistence Layer stores operational data.

Examples include:

* Request logs
* Task records
* Agent metadata
* Usage statistics

Persistence enables recovery, reporting, and observability.

---

# Observability

Observability refers to the ability to understand platform behavior through telemetry.

Observability includes:

* Logging
* Metrics
* Tracing
* Health monitoring

Observability is a platform-wide concern rather than a single subsystem.

---

# Deployment

A Deployment is an operational instance of CoreAI.

Deployments may be:

* Standalone
* Service-based
* Distributed

Deployment topology does not alter platform behavior.

---

# Design Model

CoreAI can be understood through a simple model:

```
Control Plane
      │
      ▼
Execution Plane
      │
      ▼
Provider Infrastructure
```

Protocols connect components.

Persistence records activity.

Observability provides visibility.

Together these systems form the CoreAI platform.
