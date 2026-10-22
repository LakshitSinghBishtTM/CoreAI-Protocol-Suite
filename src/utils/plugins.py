"""Plugin loading through Python entry points (opt-in).

Plugins are ordinary packages that declare entry points::

    [project.entry-points."caips.providers"]
    myprov = "my_pkg.provider:MyProvider"      # BaseProvider subclass with ``env_var``

    [project.entry-points."caips.tools"]
    mytools = "my_pkg.tools:make_tools"         # callable returning an iterable of Tool

Importing a plugin runs its code, so nothing loads unless its entry-point name is
listed in ``CAIPS_PLUGINS`` (comma separated) or passed as ``allowed``. A plugin
that fails to import or register is logged and skipped; it never stops the gateway.
"""

from __future__ import annotations

import os
from importlib.metadata import entry_points
from typing import Any, Iterable, Optional

from loguru import logger

PROVIDER_GROUP = "caips.providers"
TOOL_GROUP = "caips.tools"


def allowed_plugins(allowed: Optional[Iterable[str]] = None) -> set[str]:
    if allowed is not None:
        return {a.strip() for a in allowed if a.strip()}
    return {a.strip() for a in os.getenv("CAIPS_PLUGINS", "").split(",") if a.strip()}


def load_plugins(group: str, allowed: Optional[Iterable[str]] = None) -> dict[str, Any]:
    """Load allow-listed entry points of ``group``; returns {name: loaded object}."""
    wanted = allowed_plugins(allowed)
    loaded: dict[str, Any] = {}
    if not wanted:
        return loaded
    found = {ep.name: ep for ep in entry_points(group=group)}
    for name in sorted(wanted - found.keys()):
        logger.debug(f"plugin '{name}' not installed for group {group}")
    for name in sorted(wanted & found.keys()):
        try:
            loaded[name] = found[name].load()
        except Exception as exc:
            logger.warning(f"plugin '{name}' failed to load: {exc}")
    return loaded


def register_provider_plugins(provider_map: dict, allowed: Optional[Iterable[str]] = None) -> list[str]:
    """Add plugin providers to ``provider_map`` as (class, env_var); built-ins are never replaced."""
    added = []
    for name, cls in load_plugins(PROVIDER_GROUP, allowed).items():
        env_var = getattr(cls, "env_var", None)
        if not env_var:
            logger.warning(f"provider plugin '{name}' has no 'env_var'; skipped")
        elif name in provider_map:
            logger.warning(f"provider plugin '{name}' clashes with an existing provider; skipped")
        else:
            provider_map[name] = (cls, env_var)
            added.append(name)
    return added


def register_tool_plugins(registry: Any, allowed: Optional[Iterable[str]] = None) -> list[str]:
    """Register tools from plugin factories; they are tagged ``owner='plugin:<name>'``."""
    added = []
    for name, factory in load_plugins(TOOL_GROUP, allowed).items():
        try:
            tools = list(factory())
        except Exception as exc:
            logger.warning(f"tool plugin '{name}' failed to build tools: {exc}")
            continue
        for tool in tools:
            try:
                if tool.owner == "builtin":
                    tool.owner = f"plugin:{name}"
                registry.register(tool)  # a name clash raises: first registration wins
                added.append(tool.name)
            except Exception as exc:
                logger.warning(f"tool '{getattr(tool, 'name', '?')}' from plugin '{name}' skipped: {exc}")
    return added
