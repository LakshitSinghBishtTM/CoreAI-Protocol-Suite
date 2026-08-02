<p align="center">
  <img alt="CoreAI-Protocol-Suite Logo"
       src="assets/logo.svg"
       width="260">
</p>

<h1 align="center">CoreAI Protocol Suite</h2>
<p align="center">Enterprise-grade LLM routing and agent orchestration framework for production AI systems</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-GPL--3.0-blue.svg" alt="License"></a>
  <img src="https://img.shields.io/badge/version-1.0.0-blue" alt="Version">
  <img src="https://img.shields.io/badge/status-Production-orange" alt="Status">
  <img src="https://img.shields.io/badge/AI-Agent-purple" alt="Lean 4">
  <a href="https://github.com/LakshitSinghBishtTM/CoreAI-Protocol-Suite/graphs/contributors"><img src="https://img.shields.io/github/contributors/LakshitSinghBishtTM/CoreAI-Protocol-Suite?color=green" alt="Contributors"></a>
</p>

---

Modern AI systems are no longer limited by model capability.

They are limited by orchestration.

As deployments grow, organizations must coordinate multiple model providers, manage autonomous workflows, distribute execution across heterogeneous infrastructure, enforce operational policies, secure communication between components, and maintain visibility into increasingly complex systems.

Most AI frameworks solve a single problem.

CoreAI is designed to operate the entire system.

CoreAI Protocol Suite is a production-grade infrastructure platform that unifies provider routing, autonomous agents, distributed runtimes, protocol-governed communication, execution orchestration, and operational observability behind a coherent architectural model.

The platform treats communication, execution, routing, and coordination as first-class concerns rather than implementation details.

This allows AI systems to scale beyond isolated inference requests and evolve into coordinated, observable, and fault-tolerant infrastructure.

---

## Core Capabilities

### Multi-Provider Routing

Execute workloads across heterogeneous AI providers through a unified interface.

Supported integrations include:

* OpenAI
* Anthropic
* Gemini
* Grok
* DeepSeek

Routing decisions can incorporate:

* Cost constraints
* Availability requirements
* Latency targets
* Reliability metrics
* Runtime conditions
* Organizational policies

Applications interact with a single platform interface while CoreAI manages provider selection, failover, accounting, and execution strategy.

---

### Autonomous Agent Infrastructure

CoreAI provides persistent execution environments for long-running objectives.

Agents are capable of:

* Maintaining operational context
* Executing multi-step workflows
* Managing task lifecycles
* Coordinating with platform services
* Operating across distributed environments

Agents are treated as execution entities rather than conversational abstractions.

---

### Distributed Runtime Architecture

Execution is not constrained to a single process or host.

The runtime infrastructure supports:

* Worker registration
* Cluster coordination
* Health monitoring
* Task dispatch
* Load balancing
* Retry management

The same platform architecture can operate on a laptop, a dedicated server, or a distributed cluster.

---

### Protocol-Governed Communication

CoreAI treats protocols as architectural components.

Communication throughout the platform is governed by explicit protocol definitions rather than implementation-specific assumptions.

Built-in protocols include:

* Authentication Protocol
* Distributed Agent Protocol (DAP)
* Secure Transport Protocol (STP)

These protocols define identity, delivery guarantees, transport security, message semantics, and coordination behavior across the platform.

---

### Operational Visibility

Complex systems require visibility.

CoreAI provides integrated observability across:

* Request execution
* Provider utilization
* Runtime activity
* Agent operations
* Cost tracking
* Performance metrics
* System health

Operational data is treated as a platform capability rather than an external concern.

---

## Architecture

```text
                        Client Systems
                               │
                               ▼
                        API Layer
                               │
                               ▼
                   Orchestration Layer
             Router • Orchestrator • Scheduler
                               │
              ┌────────────────┼────────────────┐
              ▼                ▼                ▼
        Provider Layer    Agent Layer    Runtime Layer
              │                │                │
              └────────────────┼────────────────┘
                               ▼
                       Protocol Layer
                               │
                               ▼
                      Persistence Layer
```

CoreAI is organized around clear subsystem boundaries.

* The API Layer exposes platform functionality.
* The Orchestration Layer makes execution decisions.
* The Provider Layer integrates external AI services.
* The Agent Layer manages autonomous execution.
* The Runtime Layer executes workloads.
* The Protocol Layer governs communication.
* The Persistence Layer records operational state.

This separation allows individual components to evolve independently while preserving system-wide consistency.

---

## Distribution

CoreAI Protocol Suite is distributed across multiple Git hosting platforms to improve long-term availability and reduce dependence on any single provider.

* GitHub (canonical): https://github.com/lakshitsinghbishttm/CoreAI-Protocol-Suite
* GitLab: https://gitlab.com/lakshitsinghbishttm/CoreAI-Protocol-Suite
* Codeberg: https://codeberg.org/lakshitsinghbishttm/CoreAI-Protocol-Suite
* Gitea: https://gitea.com/LakshitSinghBishtTM/CoreAI-Protocol-Suite
* Bitbucket: https://bitbucket.org/lakshitsinghbishttm/coreai-protocol-suite
* SourceForge: https://sourceforge.net/projects/coreai-protocol-suite/

Mirrors are synchronized automatically through GitHub Actions.

GitHub remains the primary development repository for issue tracking, pull requests, and releases.

---

## Performance Characteristics

CoreAI includes a benchmarking script (`scripts/benchmark.py`) that runs a configurable load test against a live CoreAI server and reports latency percentiles, throughput, cost, and cache hit rate.

This section previously published specific numbers - latency percentiles, cache hit rates, cost-per-request, a per-provider cost/error table - framed as measured results. They weren't verified against a real run, so they've been removed rather than corrected: actual figures depend entirely on deployment topology, provider mix, and workload, and there's no single "CoreAI performance" number that would be honest to publish here. Run the script against your own deployment instead:

    python scripts/benchmark.py --url http://localhost:6389 --requests 100

Routing decisions may consider:

* Cost
* Availability
* Latency
* Reliability
* Context requirements
* Operational policy

---

## Design Principles

CoreAI is built around five architectural principles.

### Provider Independence

Infrastructure should never depend on a single vendor.

### Protocol-Oriented Design

Communication should be governed by explicit contracts.

### Distributed Execution

Scaling should be achieved through infrastructure expansion rather than application redesign.

### Observability by Default

Operational visibility is a requirement, not an enhancement.

### Fault-Tolerant Operation

Failures are expected and must be contained.

---

## Documentation

The platform is documented as a collection of independent architectural domains.

| Document        | Description                                      |
| --------------- | ------------------------------------------------ |
| architecture.md | System architecture and subsystem relationships  |
| protocols.md    | Communication protocols and coordination model   |
| runtime.md      | Why there's no separate runtime layer, and what handles execution instead |
| agents.md       | Agent lifecycle and autonomous execution         |
| providers.md    | Provider integrations and routing model          |
| api.md          | Public API reference                             |
| security.md     | Security architecture and controls               |
| deployment.md   | Production deployment guidance                   |
| development.md  | Engineering standards and contributor workflow   |
| concepts.md     | Core platform concepts                           |
| glossary.md     | Terminology reference                            |

---

## Version

**Current Release:** 1.0.0

CoreAI 1.0 establishes the foundational architecture for protocol-governed AI infrastructure, including:

* Multi-provider routing
* Autonomous execution
* Distributed runtimes
* Secure communication protocols
* Operational observability
* Task orchestration
* Agent coordination

Future releases will continue expanding protocol interoperability, runtime capabilities, and distributed execution features while preserving architectural consistency.

---

## License

Licensed under the GNU General Public License v3.0.

See [`LICENSE`](LICENSE) for details.

---

**Built for production AI systems. Trusted by enterprises.**