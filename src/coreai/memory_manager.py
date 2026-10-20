"""
CoreAI Protocol Suite - Memory Manager
Manages per-agent conversation history and context windows.
Handles trimming, summarization triggers, and memory persistence.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from loguru import logger


@dataclass
class Message:
    role: str  # "system" | "user" | "assistant"
    content: str
    timestamp: datetime = field(default_factory=datetime.utcnow)
    token_estimate: int = 0

    def __post_init__(self):
        if self.token_estimate == 0:
            self.token_estimate = max(1, len(self.content) // 4)


@dataclass
class ContextWindow:
    agent_id: str
    messages: list[Message] = field(default_factory=list)
    max_tokens: int = 8000  # soft limit before trimming
    system_prompt: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    last_updated: datetime = field(default_factory=datetime.utcnow)

    def token_count(self) -> int:
        total = sum(m.token_estimate for m in self.messages)
        if self.system_prompt:
            total += max(1, len(self.system_prompt) // 4)
        return total

    def is_over_limit(self) -> bool:
        return self.token_count() > self.max_tokens

    def to_list(self) -> list[dict]:
        """Serialize for provider API calls."""
        result = []
        if self.system_prompt:
            result.append({"role": "system", "content": self.system_prompt})
        for m in self.messages:
            result.append({"role": m.role, "content": m.content})
        return result


class MemoryManager:
    """
    Manages context windows per agent.
    Trims old messages when approaching token limits.
    """

    def __init__(self, default_max_tokens: int = 8000):
        self.default_max_tokens = default_max_tokens
        self._windows: dict[str, ContextWindow] = {}
        self._total_messages_added = 0
        self._total_trims = 0
        self._running = False

    async def start(self):
        self._running = True
        logger.debug("MemoryManager started")

    async def stop(self):
        self._running = False
        logger.debug("MemoryManager stopped")

    def get_or_create(
        self, agent_id: str, system_prompt: Optional[str] = None
    ) -> ContextWindow:
        """Get existing context window or create a new one."""
        if agent_id not in self._windows:
            self._windows[agent_id] = ContextWindow(
                agent_id=agent_id,
                max_tokens=self.default_max_tokens,
                system_prompt=system_prompt,
            )
            logger.debug(f"Created context window for agent {agent_id}")
        return self._windows[agent_id]

    def add_message(self, agent_id: str, role: str, content: str):
        """Append a message to an agent's context window."""
        window = self.get_or_create(agent_id)
        window.messages.append(Message(role=role, content=content))
        window.last_updated = datetime.utcnow()
        self._total_messages_added += 1

        if window.is_over_limit():
            self._trim(window)

    def get_context(self, agent_id: str) -> list[dict]:
        """Get serialized context for API calls. Returns empty list if no window."""
        window = self._windows.get(agent_id)
        if not window:
            return []
        return window.to_list()

    def set_system_prompt(self, agent_id: str, prompt: str):
        window = self.get_or_create(agent_id)
        window.system_prompt = prompt

    def clear(self, agent_id: str):
        """Wipe context for an agent (keep window, drop messages)."""
        if agent_id in self._windows:
            self._windows[agent_id].messages.clear()
            logger.debug(f"Cleared context for agent {agent_id}")

    def drop(self, agent_id: str):
        """Remove agent context window entirely."""
        self._windows.pop(agent_id, None)
        logger.debug(f"Dropped context window for agent {agent_id}")

    async def recall(self, agent_id: str, limit: int = 8) -> list[dict]:
        """
        Return up to `limit` of the agent's most recent non-system
        messages, oldest first.

        agents/autonomous_agent.py's initialize() calls this for any
        agent with the MEMORY_READ capability; it didn't exist before.
        This is genuinely "recent context", not a separate long-term
        store -- this class only ever holds the in-memory window
        (its docstring's 'memory persistence' doesn't survive a
        restart today). async for interface consistency with the rest
        of MemoryManager's lifecycle methods, though nothing here
        actually awaits I/O.
        """
        window = self._windows.get(agent_id)
        if not window:
            return []
        recent = [m for m in window.messages if m.role != "system"][-limit:]
        return [{"role": m.role, "content": m.content} for m in recent]

    async def store(self, agent_id: str, content: str) -> None:
        """
        Persist a piece of content to the agent's context window.

        agents/autonomous_agent.py's _flush_memory() calls this for
        any agent with the MEMORY_WRITE capability on shutdown; it
        didn't exist before. Stored as an assistant-role message via
        the existing add_message() rather than a separate structure,
        for the same reason as recall() above -- there is no separate
        durable store to write to yet.
        """
        self.add_message(agent_id, "assistant", content)

    def _trim(self, window: ContextWindow):
        """
        Trim oldest non-system messages until under token limit.
        Keeps at least the last 2 exchanges.
        """
        before = len(window.messages)
        min_keep = 4  # always keep last 2 user+assistant pairs

        while window.is_over_limit() and len(window.messages) > min_keep:
            window.messages.pop(0)

        trimmed = before - len(window.messages)
        if trimmed > 0:
            self._total_trims += trimmed
            logger.debug(
                f"Trimmed {trimmed} message(s) from agent {window.agent_id} "
                f"(tokens: {window.token_count()}/{window.max_tokens})"
            )

    def stats(self) -> dict:
        return {
            "active_windows": len(self._windows),
            "total_messages_added": self._total_messages_added,
            "total_trims": self._total_trims,
            "windows": {
                aid: {
                    "messages": len(w.messages),
                    "tokens": w.token_count(),
                    "max_tokens": w.max_tokens,
                    "last_updated": w.last_updated.isoformat(),
                }
                for aid, w in self._windows.items()
            },
        }
