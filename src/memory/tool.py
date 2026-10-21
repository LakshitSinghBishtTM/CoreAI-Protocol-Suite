"""Memory tools for agents, bound to one owner and namespace (the model can't pick others).

Recalled text is wrapped as untrusted: memory may hold content a user or a web
page once supplied, so it must never act as instructions.
"""

from guardrails.injection import wrap_untrusted
from tools import Permission, Tool

from .store import MemoryStore


def memory_tools(store: MemoryStore, owner: str, ns: str = "default") -> list[Tool]:
    def remember(key: str, value: str) -> str:
        store.remember(owner, key, value, ns)
        return f"Stored '{key}'."

    def search(query: str, limit: int = 5) -> str:
        hits = store.search(owner, query, ns, max(1, min(int(limit), 20)))
        if not hits:
            return "No matching memories."
        return "\n".join(
            f"[{i}] {h['key']}: " + wrap_untrusted(h["value"], h["key"], "memory")
            for i, h in enumerate(hits, 1)
        )

    def forget(key: str) -> str:
        n = store.forget(owner, key, ns)
        return f"Forgot '{key}'." if n else f"No memory named '{key}'."

    def obj(props: dict, required: list[str]) -> dict:
        return {
            "type": "object",
            "properties": props,
            "required": required,
            "additionalProperties": False,
        }

    s = {"type": "string", "minLength": 1}
    return [
        Tool(
            "memory_remember",
            "Save a fact to long-term memory under a short key.",
            obj({"key": {**s, "maxLength": 200}, "value": s}, ["key", "value"]),
            remember,
            permission=Permission.WRITE,
        ),
        Tool(
            "memory_search",
            "Search long-term memory by keywords.",
            obj(
                {"query": s, "limit": {"type": "integer", "minimum": 1, "maximum": 20}},
                ["query"],
            ),
            search,
            permission=Permission.READ_ONLY,
        ),
        Tool(
            "memory_forget",
            "Delete one memory by key.",
            obj({"key": s}, ["key"]),
            forget,
            permission=Permission.DESTRUCTIVE,
        ),
    ]
