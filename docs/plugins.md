# Plugins

Providers and tools can ship as separate packages and be discovered through Python entry points.

```toml
# the plugin's pyproject.toml
[project.entry-points."caips.providers"]
myprov = "my_pkg.provider:MyProvider"   # BaseProvider subclass with an `env_var` attribute

[project.entry-points."caips.tools"]
mytools = "my_pkg.tools:make_tools"     # callable returning an iterable of Tool
```

## Enabling

Importing a plugin runs its code, so **nothing loads by default**. Name each plugin you trust:

- environment: `CAIPS_PLUGINS=myprov,mytools`
- tools config: `plugins: [mytools]` (overrides the environment for tools)

## Rules

- A provider plugin needs an `env_var`; it loads when that variable is set, like a built-in. It cannot replace a built-in provider.
- Tools from plugins get `owner = "plugin:<name>"`. A tool whose name is already registered is skipped (first registration wins).
- A plugin that fails to import or register is logged and skipped; the gateway keeps starting.
- Plugin tools go through the same permissions, approvals and audit log as built-in tools.

Guardrail and routing-strategy plugins are not implemented yet.
