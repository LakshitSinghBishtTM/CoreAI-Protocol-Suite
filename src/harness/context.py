"""harness/context.py: keep the prompt inside the window.

Pinned: the system prompt (sent separately) and the first user message (the
task). Compaction order: shrink old tool outputs, then replace the oldest
whole turns with one summary note. A tool result is never separated from the
assistant message that called it.
"""

from dataclasses import replace
from typing import Awaitable, Callable, Optional

from providers.base import Message
from utils.tokens import estimate_tokens

OLD_TOOL_OUTPUT_CHARS = 300
SUMMARY_PREFIX = "[Earlier steps, condensed] "


def count(messages: list[Message], system: str = "") -> int:
    n = estimate_tokens(system) if system else 0
    for m in messages:
        n += estimate_tokens(m.content or "") + 4
        for c in m.tool_calls or []:
            n += estimate_tokens(str(c.get("arguments", ""))) + 8
    return n


def _turns(messages: list[Message]) -> list[list[Message]]:
    """Group into turns so tool calls stay with their results."""
    turns: list[list[Message]] = []
    for m in messages:
        if m.role == "tool" and turns:
            turns[-1].append(m)
        else:
            turns.append([m])
    return turns


def digest(dropped: list[Message]) -> str:
    tools = [c.get("name", "?") for m in dropped for c in (m.tool_calls or [])]
    parts = [f"{len(dropped)} messages removed"]
    if tools:
        parts.append("tools used: " + ", ".join(dict.fromkeys(tools)))
    return SUMMARY_PREFIX + "; ".join(parts) + "."


async def compact(
    messages: list[Message],
    *,
    system: str,
    max_tokens: int,
    keep_recent: int = 6,
    summarizer: Optional[Callable[[list[Message]], Awaitable[str]]] = None,
) -> tuple[list[Message], bool]:
    """Return (messages that fit, whether anything changed). Pure: input untouched."""
    if count(messages, system) <= max_tokens:
        return messages, False
    msgs = list(messages)
    pinned, rest = msgs[:1], msgs[1:]
    # 1) shrink tool outputs outside the recent window
    cut = max(0, len(rest) - keep_recent)
    for i in range(cut):
        m = rest[i]
        if m.role == "tool" and len(m.content) > OLD_TOOL_OUTPUT_CHARS:
            rest[i] = replace(
                m, content=m.content[:OLD_TOOL_OUTPUT_CHARS] + " …[truncated]"
            )
    if count(pinned + rest, system) <= max_tokens:
        return pinned + rest, True
    # 2) drop oldest whole turns, keeping at least the recent window
    turns = _turns(rest)
    dropped: list[Message] = []

    def _over() -> bool:
        flat = [m for t in turns for m in t]
        return (
            count(pinned + [Message("user", SUMMARY_PREFIX + "x" * 80)] + flat, system)
            > max_tokens
        )

    while (
        len(turns) > 1
        and sum(len(t) for t in turns[1:]) >= min(keep_recent, 1)
        and _over()
    ):
        dropped.extend(turns.pop(0))
    if dropped:
        note = digest(dropped)
        if summarizer is not None:
            try:
                note = SUMMARY_PREFIX + (await summarizer(dropped)).strip()
            except Exception:
                pass  # fall back to the deterministic digest
        rest = [Message("user", note)] + [m for t in turns for m in t]
    else:
        rest = [m for t in turns for m in t]
    # a leading tool message with no caller is invalid for providers
    while rest and rest[0].role == "tool":
        rest.pop(0)
    return pinned + rest, True
