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
| `web_search` | `web_search:` | queries one configured JSON endpoint (e.g. SearXNG); at most 10 results, snippets capped |
| `db_query` | `databases:` | one SELECT only; file opened read-only plus SQLite authorizer; 200 rows; runaway queries cut off |
| `shell_run` | `shell:` | allow-listed programs only, no shell (no pipes or substitution), empty environment, 20 s timeout, **always needs approval** and `max_permission: destructive` |

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

## Approval webhook
Set `approval_webhook` (`url`, `secret_env`, optional `include_arguments`) to be notified when a
destructive call is waiting. The body is signed: `X-CaiPS-Signature` is
`HMAC-SHA256(secret, "<X-CaiPS-Timestamp>.<body>")`. Argument values are left out unless
`include_arguments: true`; reviewers read them from `GET /v1/tools/approvals`. A missing secret is a
startup error (no unsigned webhooks), and a failed delivery never blocks approvals.

## Tracing and replay
Each call runs in a `caips.tool` span (tool, caller, permission, owner, outcome, duration; never
argument values or output). For regression tests, set `registry.on_call = Recorder("calls.jsonl")`
in development and later run `await replay(registry, "calls.jsonl")`; it returns the calls whose
result changed. Recordings contain argument values and output, so do not enable them in production.

## Using external MCP servers
List them under `mcp_servers:` (stdio `command` or HTTP `url`). Their tools join the registry as
`<server>__<tool>`, so they can never replace a built-in. Rules:
- **Deny by default:** only tools in the server's `allow` list are registered (`"*"` for all).
- **Grants still apply:** a subject needs `server__tool` (or `"*"`) in `grants` to use one.
- **Untrusted metadata:** descriptions are flattened and capped at 500 characters; bad names and
  invalid schemas are skipped. Arguments are validated locally before they reach the server.
- **Permission:** remote tools default to `write` (not `read_only`) unless config says otherwise.
- **Clean child environment:** a stdio server gets `PATH`/`HOME` and the `env:` you list, so
  provider keys in the gateway's environment are not passed on.
- A server that fails to connect is logged and skipped; startup continues. Servers are connected
  at startup only.

## Not built yet
Sandboxed code execution (needs a container or microVM design), reconnecting MCP servers that
drop, and a CLI wrapper for replay. `shell_run` is not a sandbox: it limits what can start, not what
an allowed program can do, so allow only programs you would let the approver run themselves.
