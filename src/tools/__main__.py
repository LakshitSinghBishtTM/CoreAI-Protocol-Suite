"""``python -m tools``: serve the tool registry over MCP stdio.

Reads config/tools.yml (or $COREAI_TOOLS_FILE). The stdio caller is the
subject ``stdio``; give it tools under ``grants.stdio`` (or ``default``).
"""

import asyncio
import sys

from .config import load_tools_runtime
from .mcp import serve_stdio


def main() -> int:
    rt = load_tools_runtime()
    if rt is None or not rt.enabled:
        print("No tools config found (config/tools.yml).", file=sys.stderr)
        return 1
    asyncio.run(serve_stdio(rt.mcp, caller="stdio"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
