"""tools/config.py

Server-side tool configuration (``config/tools.yml``, or ``$COREAI_TOOLS_FILE``).
Clients never choose their own tools: grants map an authenticated subject to the
tool names it may use, and anything not granted is denied.

    enabled: true
    workspace: ./workspace          # enables file_read / file_write
    http_allowed_domains: [example.com]   # enables http_fetch
    web_search:                     # enables web_search (one JSON endpoint, e.g. SearXNG)
      url: https://search.example/search
      params: {format: json}
      results_path: results
      api_key_env: SEARCH_API_KEY   # optional bearer token from the environment
    databases: {app: ./data/app.db}   # enables db_query (read-only SELECT)
    shell:                          # enables shell_run (always needs human approval)
      allowed_commands: [git, ls]
    python_sandbox: {enabled: true}   # enables python_run (needs approval, no network)
    max_permission: write           # read_only | write | destructive
    rate_per_minute: {file_write: 30}
    grants:
      default: [calculator]         # subjects with no entry of their own
      alice: ["*"]
    approval_webhook:               # optional: notify when a destructive call waits
      url: https://example.com/hook
      secret_env: TOOLS_WEBHOOK_SECRET   # signing secret, from the environment
      include_arguments: false      # default: names only, no values
    mcp_servers:                    # external MCP servers whose tools to use
      files:
        command: ["npx", "some-mcp-server"]   # or  url: https://host/mcp
        allow: [read_file]          # deny by default; "*" for all
        permission: write           # default write; set read_only only if you trust it
        env: {}                     # child gets only PATH/HOME plus this
      # grant subjects the namespaced name: grants: {alice: [files__read_file]}
"""

import os
from dataclasses import dataclass, field
from typing import Optional

from .approvals import ApprovalStore
from .builtin import HttpFetcher, Workspace, register_builtins
from .mcp import McpServer
from .registry import Permission, ToolRegistry


@dataclass
class ToolsRuntime:
    registry: ToolRegistry
    approvals: ApprovalStore
    mcp: McpServer
    grants: dict = field(default_factory=dict)
    mcp_servers: dict = field(default_factory=dict)
    max_permission: Permission = Permission.WRITE
    enabled: bool = True

    def allowed_for(self, subject: str) -> list[str]:
        g = self.grants.get(subject, self.grants.get("default", []))
        return list(g or [])


def _register_extras(registry: ToolRegistry, cfg: dict) -> None:
    from .extra import ShellRunner, SqliteReader, WebSearch, register_extras

    search = db = shell = None
    ws = cfg.get("web_search") or {}
    if ws.get("url"):
        headers = dict(ws.get("headers") or {})
        if ws.get("api_key_env"):  # key from the environment, sent as a bearer token
            headers["Authorization"] = "Bearer " + os.getenv(ws["api_key_env"], "")
        search = WebSearch(
            ws["url"], ws.get("query_param", "q"), ws.get("params"), headers,
            ws.get("results_path", "results"), int(ws.get("max_results", 5)),
        )  # fmt: skip
    if cfg.get("databases"):
        db = SqliteReader(cfg["databases"])
    sh = cfg.get("shell") or {}
    if sh.get("allowed_commands"):
        shell = ShellRunner(
            sh["allowed_commands"], sh.get("cwd") or cfg.get("workspace") or "."
        )
    register_extras(registry, search, db, shell)
    sb = cfg.get("python_sandbox") or {}
    if sb.get("enabled"):
        from .sandbox import PythonSandbox, register_sandbox

        register_sandbox(registry, PythonSandbox(
            float(sb.get("timeout_s", 10)), int(sb.get("memory_mb", 256)),
            int(sb.get("cpu_s", 10)), bool(sb.get("allow_network", False)),
        ))  # fmt: skip


def build_runtime(config: Optional[dict]) -> ToolsRuntime:
    cfg = config or {}
    registry = ToolRegistry()
    workspace = Workspace(cfg["workspace"]) if cfg.get("workspace") else None
    domains = cfg.get("http_allowed_domains") or []
    fetcher = HttpFetcher(domains) if domains else None
    register_builtins(registry, workspace, fetcher)
    _register_extras(registry, cfg)
    for name, per_min in (cfg.get("rate_per_minute") or {}).items():
        tool = registry.get(name)
        if tool is not None:
            tool.rate_per_minute = int(per_min)
    try:
        max_perm = Permission[str(cfg.get("max_permission", "write")).upper()]
    except KeyError:
        raise ValueError("max_permission must be read_only, write or destructive")
    grants = {str(k): list(v or []) for k, v in (cfg.get("grants") or {}).items()}
    approvals = ApprovalStore()
    hook = cfg.get("approval_webhook") or {}
    if hook.get("url"):
        from .webhook import ApprovalWebhook

        secret = os.getenv(hook.get("secret_env", "TOOLS_WEBHOOK_SECRET"), "")
        approvals.on_request = ApprovalWebhook(
            hook["url"], secret, bool(hook.get("include_arguments", False))
        )  # raises if the secret is missing: no unsigned webhooks
    rt = ToolsRuntime(
        registry=registry, approvals=approvals, mcp=None,  # type: ignore[arg-type]
        grants=grants, mcp_servers=dict(cfg.get("mcp_servers") or {}), max_permission=max_perm, enabled=bool(cfg.get("enabled", True)),
    )  # fmt: skip
    rt.mcp = McpServer(registry, rt.allowed_for, approvals, max_perm)
    return rt


def load_tools_runtime(path: Optional[str] = None) -> Optional[ToolsRuntime]:
    """None when there is no config file: the tools layer is opt-in."""
    path = path or os.getenv("COREAI_TOOLS_FILE", "config/tools.yml")
    if not os.path.exists(path):
        return None
    import yaml

    with open(path) as fh:
        return build_runtime(yaml.safe_load(fh) or {})
