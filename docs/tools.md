# Tools layer

CaiPS can host tools and expose them to models (through the gateway) and to any MCP client.
The layer is **opt-in**: with no `config/tools.yml` there is no tools endpoint and `run_tools`
is refused. Start from `config/tools.example.yml`.

## Who can use what
Access is decided on the server. `grants` maps the authenticated subject (API key or token
subject) to tool names, with `default` for subjects without an entry. Nothing is granted unless
listed (`"*"` means all). Clients cannot pick their own tools. `max_permission` caps the
permission level (`read_only`, `write`, `destructive`) for everyone.

## Built-in tools
| Tool | Enabled by | Limits |
| --- | --- | --- |
| `calculator` | always | arithmetic only, no `eval`, bounded exponents |
| `file_read`, `file_write` | `workspace:` | stays inside the folder (no `..`, no symlink escape), size capped |
| `http_fetch` | `http_allowed_domains:` | allow-listed domains, public addresses only, no redirects, size capped |

## Every call is checked
Allow list, argument schema, permission cap, approval (destructive tools), per-tool rate limit
(`rate_per_minute`), timeout and a 20,000-character output cap. Output shown to a model is wrapped
in a `<tool_output trust="untrusted">` label. Every call is written to the audit log (`tool.call`: who, which tool, outcome, duration and
argument *names* only, never values). Approval decisions are logged as `tool.approval`.

## Approvals
A destructive tool does not run until a human approves that exact call (same caller, tool and
arguments). Approvals are single-use, expire after 15 minutes and are held in memory.

- `GET /v1/tools/approvals` lists pending requests (admin scope).
- `POST /v1/tools/approvals/{id}` with `{"approve": true|false}` decides.
- Then repeat the original request or MCP call.

## Gateway execution
Send `"run_tools": true` to `/v1/chat/completions`. The gateway offers the caller's granted tools,
runs the model's tool calls and continues until the model answers. Limits: 5 tool steps. The reply
lists steps in `caips_tool_steps`. Streaming is buffered. A request that needs approval returns 409
`approval_required`.

## MCP
- HTTP: `POST /mcp` (JSON-RPC 2.0; scope `tools:call`). Supports `initialize`, `ping`,
  `tools/list`, `tools/call`.
- stdio: `python -m tools` (caller subject `stdio`).

## Not built yet
MCP client (using external servers' tools), web search, sandboxed code execution, database and
shell tools, tool-call replay harness, tracing spans for tool calls, approval webhooks.
