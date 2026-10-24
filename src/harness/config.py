"""harness/config.py: agent definitions as config."""

from dataclasses import dataclass, field, fields
from typing import Optional

# Small local models get tighter limits (roadmap: shorter plans, lower caps).
LOCAL_CAPS = {"max_steps": 8, "context_tokens": 6000, "max_tokens": 512}


@dataclass
class AgentDef:
    name: str
    system_prompt: str
    model: Optional[str] = (
        None  # or a routing policy name, passed through to the router
    )
    allowed_tools: list[str] = field(default_factory=list)
    max_steps: int = 20
    max_cost_usd: Optional[float] = None
    max_total_tokens: Optional[int] = None
    deadline_s: Optional[float] = None
    context_tokens: int = 24000
    keep_recent: int = 6  # messages never compacted
    max_tokens: int = 1024  # per model call
    temperature: float = 0.2
    stall_repeats: int = 3  # identical consecutive tool calls before stopping
    approval_tools: list[str] = field(default_factory=list)  # always need a human
    local: bool = False  # backing model is a small local model
    summarize: bool = False  # use the model to summarize dropped context
    ask_when_uncertain: bool = False  # offer request_human_review to the model
    memory_ns: Optional[str] = None  # recall this memory namespace into the prompt
    memory_recall: int = 5  # max memories recalled per model call

    @classmethod
    def from_dict(cls, d: dict) -> "AgentDef":
        known = {f.name for f in fields(cls)}
        extra = set(d) - known
        if extra:
            raise ValueError(f"unknown agent fields: {sorted(extra)}")
        return cls(**d)

    def effective(self) -> "AgentDef":
        """Apply local-model caps (only ever lowers limits)."""
        if not self.local:
            return self
        from dataclasses import replace

        return replace(
            self, **{k: min(getattr(self, k), v) for k, v in LOCAL_CAPS.items()}
        )
