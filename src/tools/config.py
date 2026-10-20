"""tools/config.py

Server-side tool configuration (``config/tools.yml``, or ``$COREAI_TOOLS_FILE``).
Clients never choose their own tools: grants map an authenticated subject to the
tool names it may use, and anything not granted is denied.

    enabled: true
    workspace: ./workspace          # enables file_read / file_write
    http_allowed_domains: [example.com]   # enables http_fetch
    max_permission: write           # read_only | write | destructive
    rate_per_minute: {file_write: 30}
    grants:
      default: [calculator]         # subjects with no entry of their own
      alice: ["*"]
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
    max_permission: Permission = Permission.WRITE
    enabled: bool = True

    def allowed_for(self, subject: str) -> list[str]:
        g = self.grants.get(subject, self.grants.get("default", []))
        return list(g or [])


def build_runtime(config: Optional[dict]) -> ToolsRuntime:
    cfg = config or {}
    registry = ToolRegistry()
    workspace = Workspace(cfg["workspace"]) if cfg.get("workspace") else None
    domains = cfg.get("http_allowed_domains") or []
    fetcher = HttpFetcher(domains) if domains else None
    register_builtins(registry, workspace, fetcher)
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
    rt = ToolsRuntime(
        registry=registry, approvals=approvals, mcp=None,  # type: ignore[arg-type]
        grants=grants, max_permission=max_perm, enabled=bool(cfg.get("enabled", True)),
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
