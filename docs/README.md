# CaiPS documentation

Start here, then follow the guide for what you are doing. For setup and a first request, see the [README](../README.md).

| Guide | What it covers |
| --- | --- |
| [concepts.md](concepts.md), [architecture.md](architecture.md), [glossary.md](glossary.md) | How the pieces fit and the terms used |
| [api.md](api.md) | HTTP API: native, OpenAI-compatible and Anthropic-compatible routes, scopes, errors |
| [providers.md](providers.md), [local-models.md](local-models.md) | Cloud providers, local servers and managed GGUF models; adding a provider |
| [routing.md](routing.md), [budgets.md](budgets.md) | Routing policies and per-key and per-model budgets |
| [tools.md](tools.md), [agents.md](agents.md), [runtime.md](runtime.md) | Tools and MCP, the agent harness, and what executes requests |
| [operations.md](operations.md), [events.md](events.md), [deployment.md](deployment.md) | Logs, metrics, tracing, alerts, Docker, Kubernetes and Helm |
| [security.md](security.md), [protocols.md](protocols.md) | Security model; the experimental, unaudited protocols |
| [cli.md](cli.md), [evals.md](evals.md), [plugins.md](plugins.md) | `caips` CLI, eval runner, entry-point plugins |
| [development.md](development.md), [roadmap.md](roadmap.md) | Contributing workflow and the plan |

The server listens on port 6969, and the `caips` CLI, the TUI and the SDKs default to it. Set `CAIPS_URL` (or pass a base URL) for another host or port.
