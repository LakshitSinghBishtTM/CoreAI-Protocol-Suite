# Glossary

| Term                     | Definition                                                                                  |
| ------------------------ | ------------------------------------------------------------------------------------------- |
| Agent                    | Persistent execution entity capable of pursuing objectives                                  |
| API Layer                | External interface through which clients access CoreAI services                             |
| Authentication Protocol  | Protocol responsible for identity verification and access control                           |
| Capability               | Function or operation an agent can perform                                                  |
| Control Plane            | Platform components responsible for decision-making and coordination                        |
| Coordinator              | Distributed runtime component responsible for worker management and task dispatch           |
| DAP                      | Distributed Agent Protocol used for coordination and messaging                              |
| Deployment               | Operational instance of CoreAI                                                              |
| Distributed Runtime      | Runtime subsystem supporting execution across multiple nodes                                |
| Execution Plane          | Platform components responsible for performing work                                         |
| Objective                | Desired outcome assigned to an agent                                                        |
| Orchestrator             | Component responsible for coordinating tasks and workflows                                  |
| Persistence Layer        | Subsystem responsible for storing operational data                                          |
| Platform                 | Complete CoreAI deployment and associated services                                          |
| Protocol                 | Formal communication contract between components                                            |
| Provider                 | External AI service integrated into CoreAI                                                  |
| Provider Layer           | Abstraction layer responsible for provider integrations                                     |
| Request                  | Individual unit of client-initiated work                                                    |
| Router                   | Component responsible for provider selection                                                |
| Runtime                  | Infrastructure responsible for executing workloads                                          |
| Scheduler                | Component responsible for recurring and background operations                               |
| STP                      | Secure Transport Protocol providing transport-level security services                       |
| Task                     | Unit of work executed by an agent                                                           |
| Task Orchestrator        | Component responsible for task assignment and tracking                                      |
| Worker                   | Runtime node capable of executing workloads                                                 |
| Worker Pool              | Collection of workers managed by a coordinator                                              |
| Observability            | Collection of metrics, logs, traces, and monitoring data used to understand system behavior |
| Usage Analytics          | Operational metrics related to requests, costs, performance, and utilization                |
| Routing Strategy         | Policy used by the router when selecting providers                                          |
| Context Window           | Retained execution context associated with an agent                                         |
| Runtime Engine           | Core execution pipeline responsible for processing workloads                                |
| Message Envelope         | Structured metadata surrounding a protocol message                                          |
| Heartbeat                | Periodic signal used to verify worker or service health                                     |
| Load Balancing           | Distribution of workloads across available execution resources                              |
| Retry Policy             | Rules governing re-execution after failures                                                 |
| Session                  | Authenticated communication context established between components                          |
| Telemetry                | Operational data emitted by platform components                                             |
| Trace ID                 | Unique identifier used to correlate activity across services                                |
| Health Check             | Endpoint or mechanism used to determine service status                                      |
| Cost Accounting          | Tracking and attribution of execution costs                                                 |
| Token Accounting         | Measurement and estimation of token usage across providers                                  |
| Resource Limit           | Configured constraint protecting runtime capacity                                           |
| Horizontal Scaling       | Increasing capacity by adding additional nodes or services                                  |
| Vertical Scaling         | Increasing capacity by allocating additional resources to existing nodes                    |
| High Availability        | Deployment strategy designed to minimize downtime and service disruption                    |
| Fault Tolerance          | Ability to continue operating despite component failures                                    |
| Provider Independence    | Architectural principle preventing coupling to a specific AI vendor                         |
| Protocol-Oriented Design | Architectural approach based on explicit communication contracts                            |
| Distributed Execution    | Execution model spanning multiple workers or nodes                                          |
