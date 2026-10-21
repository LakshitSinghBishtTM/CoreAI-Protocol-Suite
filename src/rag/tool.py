"""Retrieval tool: any agent can search a collection and get cited, untrusted-wrapped passages."""

from guardrails.injection import wrap_untrusted
from tools import Permission, Tool

from .store import VectorStore


def retrieval_tool(store: VectorStore, owner: str, collection: str, k: int = 4) -> Tool:
    """A read-only tool bound to one owner and collection (the model cannot pick others)."""

    async def handler(query: str) -> str:
        hits = await store.search(owner, collection, query, k=k)
        if not hits:
            return "No relevant passages found."
        return (
            "\n".join(
                f"[{i}] "
                + wrap_untrusted(
                    h["text"], f"{h['source']}#{h['chunk']}", "retrieved_document"
                )
                for i, h in enumerate(hits, 1)
            )
            + "\nCite passages as [n] with their source."
        )

    return Tool(
        name=f"search_{collection}".replace("-", "_"),
        description=f"Search the '{collection}' document collection. Returns numbered passages with sources.",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string", "minLength": 1}},
            "required": ["query"],
            "additionalProperties": False,
        },
        handler=handler,
        permission=Permission.READ_ONLY,
    )
