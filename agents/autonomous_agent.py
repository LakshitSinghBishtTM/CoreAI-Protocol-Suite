"""
agents/autonomous_agent.py

Core autonomous agent implementation for CoreAI.
Each agent maintains its own context window, tool access, and decision loop.
"""

import asyncio
import logging
import time
from typing import Any, Callable, Dict, List, Optional

from coreai.kernel import Kernel
from coreai.memory_manager import MemoryManager
from coreai.retry import retry_with_backoff

logger = logging.getLogger(__name__)


class AgentCapability:
    WEB_SEARCH = "web_search"
    CODE_EXEC = "code_exec"
    FILE_IO = "file_io"
    API_CALLS = "api_calls"
    MEMORY_READ = "memory_read"
    MEMORY_WRITE = "memory_write"
    AGENT_SPAWN = "agent_spawn"


class AgentContext:
    """Rolling context window for a single agent session."""

    MAX_MESSAGES = 64

    def __init__(self):
        self.messages: List[Dict] = []
        self.metadata: Dict = {}
        self.token_count: int = 0

    def append(self, role: str, content: str) -> None:
        self.messages.append({"role": role, "content": content})
        if len(self.messages) > self.MAX_MESSAGES:
            # Drop oldest non-system messages
            self.messages = [m for m in self.messages if m["role"] == "system"][
                :1
            ] + self.messages[-(self.MAX_MESSAGES - 1) :]

    def clear(self) -> None:
        self.messages = []
        self.token_count = 0


class AutonomousAgent:
    """
    A single autonomous agent capable of multi-step reasoning,
    tool use, and self-directed task execution.
    """

    STEP_LIMIT = 24
    IDLE_TIMEOUT = 300  # seconds before context is cleared

    def __init__(
        self,
        agent_id: str,
        name: str,
        capabilities: List[str],
        kernel: Kernel,
        memory: MemoryManager,
        config: Optional[Dict] = None,
    ):
        self.agent_id = agent_id
        self.name = name
        self.capabilities = set(capabilities)
        self.kernel = kernel
        self.memory = memory
        self.config = config or {}

        self.context = AgentContext()
        self.tools: Dict[str, Callable] = {}
        self._paused = asyncio.Event()
        self._paused.set()  # not paused by default
        self._last_active = time.monotonic()

        self.system_prompt = self.config.get(
            "system_prompt",
            (
                f"You are {self.name}, an autonomous agent in the CoreAI protocol suite. "
                "You have access to a set of tools and must complete tasks methodically. "
                "Think step by step. When uncertain, ask for clarification. "
                "Never fabricate tool results."
            ),
        )

        logger.debug(
            "AutonomousAgent %s created with capabilities: %s",
            self.agent_id,
            self.capabilities,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Bootstrap agent: load memory, register tools, warm up kernel."""
        logger.info("Initializing agent %s", self.agent_id)

        self.context.append("system", self.system_prompt)

        if AgentCapability.MEMORY_READ in self.capabilities:
            prior = await self.memory.recall(self.agent_id, limit=8)
            for item in prior:
                self.context.append("assistant", f"[memory] {item['content']}")

        await self._register_tools()
        await self.kernel.warm_up(self.agent_id)
        self._last_active = time.monotonic()
        logger.info("Agent %s initialized", self.agent_id)

    async def shutdown(self) -> None:
        logger.info("Shutting down agent %s", self.agent_id)
        if AgentCapability.MEMORY_WRITE in self.capabilities:
            await self._flush_memory()
        self.context.clear()

    async def pause(self) -> None:
        self._paused.clear()
        logger.debug("Agent %s paused", self.agent_id)

    async def resume(self) -> None:
        self._paused.set()
        logger.debug("Agent %s resumed", self.agent_id)

    async def health_check(self) -> bool:
        idle_for = time.monotonic() - self._last_active
        if idle_for > self.IDLE_TIMEOUT:
            logger.warning(
                "Agent %s idle for %.0fs, clearing context", self.agent_id, idle_for
            )
            self.context.clear()
            self.context.append("system", self.system_prompt)
        return await self.kernel.ping(self.agent_id)

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def execute(self, task: Dict) -> Any:
        """
        Run an autonomous task loop until completion or step limit.
        task = {"instruction": str, "context": dict, "constraints": list}
        """
        await self._paused.wait()
        self._last_active = time.monotonic()

        instruction = task.get("instruction", "")
        constraints = task.get("constraints", [])
        extra_ctx = task.get("context", {})

        logger.info("Agent %s executing: %s", self.agent_id, instruction[:80])

        self.context.append(
            "user", self._build_prompt(instruction, constraints, extra_ctx)
        )

        steps = 0
        result = None

        while steps < self.STEP_LIMIT:
            await self._paused.wait()
            steps += 1

            response = await retry_with_backoff(
                self.kernel.complete,
                agent_id=self.agent_id,
                messages=self.context.messages,
                tools=list(self.tools.keys()),
            )

            self.context.append("assistant", response.content)

            if response.is_final:
                result = response.content
                break

            if response.tool_call:
                tool_result = await self._invoke_tool(
                    response.tool_call["name"],
                    response.tool_call["args"],
                )
                self.context.append("user", f"[tool_result] {tool_result}")

        if steps >= self.STEP_LIMIT:
            logger.warning(
                "Agent %s hit step limit (%d)", self.agent_id, self.STEP_LIMIT
            )

        self._last_active = time.monotonic()
        return result

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------

    async def _register_tools(self) -> None:
        from coreai.router import ToolRouter

        router = ToolRouter(self.capabilities)
        self.tools = await router.get_tools_for_agent(self.agent_id)
        logger.debug("Agent %s registered %d tools", self.agent_id, len(self.tools))

    async def _invoke_tool(self, tool_name: str, args: Dict) -> Any:
        if tool_name not in self.tools:
            return f"[error] Unknown tool: {tool_name}"
        try:
            return await self.tools[tool_name](**args)
        except Exception as exc:
            logger.error(
                "Tool %s error for agent %s: %s", tool_name, self.agent_id, exc
            )
            return f"[error] {tool_name} failed: {exc}"

    def register_tool(self, name: str, fn: Callable) -> None:
        self.tools[name] = fn
        logger.debug("Agent %s registered custom tool: %s", self.agent_id, name)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_prompt(self, instruction: str, constraints: List, context: Dict) -> str:
        parts = [f"Task: {instruction}"]
        if constraints:
            parts.append("Constraints:\n" + "\n".join(f"- {c}" for c in constraints))
        if context:
            import json

            parts.append("Context:\n" + json.dumps(context, indent=2))
        return "\n\n".join(parts)

    async def _flush_memory(self) -> None:
        recent = [
            m["content"]
            for m in self.context.messages
            if m["role"] == "assistant" and not m["content"].startswith("[memory]")
        ][-4:]
        for item in recent:
            await self.memory.store(self.agent_id, item)

    def __repr__(self) -> str:
        return f"<AutonomousAgent id={self.agent_id} name={self.name}>"
