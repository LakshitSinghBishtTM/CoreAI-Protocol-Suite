import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncGenerator, Optional

from loguru import logger


@dataclass
class Message:
    role: str  # "user", "assistant", "system", "tool"
    content: str
    # Normalised tool calls an assistant message made:
    # [{"id": str, "name": str, "arguments": "<JSON string>"}].
    tool_calls: Optional[list[dict]] = None
    # For role "tool": the id of the call this message answers.
    tool_call_id: Optional[str] = None
    # Images on a user message: [{"media_type": "image/png", "data": "<base64>"}]
    # for inline data or [{"url": "https://..."}] for a remote image.
    images: Optional[list[dict]] = None


def _count(value) -> int | None:
    """A real, non-negative token count, or None (mocks, missing, junk)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) if value >= 0 else None


@dataclass
class StreamUsage:
    """Usage for one streamed request.

    Providers call ``report()`` when the SDK gives token counts. The router
    fills in the rest when the stream ends: an estimate if nothing was
    reported (``estimated``), then ``cost_usd`` and the serving ``provider``.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    reported: bool = False  # the provider's SDK supplied the token counts
    estimated: bool = False  # the router estimated them from text length
    completed: bool = False  # the stream ended normally
    cost_usd: float = 0.0
    provider: str = ""
    opened_model: str = ""  # model of the provider that opened the stream
    finish_reason: Optional[str] = None  # normalised: stop | length | content_filter

    def report(self, input_tokens, output_tokens, model=None) -> None:
        i, o = _count(input_tokens), _count(output_tokens)
        if i is None and o is None:
            return
        self.input_tokens = i if i is not None else self.input_tokens
        self.output_tokens = o if o is not None else self.output_tokens
        if isinstance(model, str) and model:
            self.model = model
        self.reported = True


@dataclass
class CompletionRequest:
    messages: list[Message]
    model: Optional[str] = None
    max_tokens: int = 1024
    temperature: float = 0.7
    stream: bool = False
    system_prompt: Optional[str] = None
    metadata: dict = field(default_factory=dict)
    # Tools in OpenAI's shape: [{"type": "function", "function": {"name",
    # "description", "parameters": <JSON schema>}}]. Adapters translate.
    tools: Optional[list[dict]] = None
    # "auto" | "none" | "required" | {"type": "function", "function": {"name"}}
    tool_choice: Optional[object] = None
    # OpenAI's response_format: {"type": "json_object"} or
    # {"type": "json_schema", "json_schema": {"name", "schema", ...}}
    response_format: Optional[dict] = None
    # Set by the router for streams so providers can report token usage.
    stream_usage: Optional[StreamUsage] = field(default=None, repr=False, compare=False)

    @property
    def cacheable(self) -> bool:
        """Tool conversations are never served from, or stored in, the cache:
        the cache key only covers role and content."""
        return not self.tools and not any(
            m.tool_calls or m.images or m.role == "tool" for m in self.messages
        )


@dataclass
class CompletionResponse:
    content: str
    model: str
    provider: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: float
    cached: bool = False
    metadata: dict = field(default_factory=dict)


VALID_ROLES = ("system", "user", "assistant", "tool")


def normalize_finish_reason(raw) -> Optional[str]:
    """Map a provider's stop reason onto the OpenAI vocabulary."""
    if raw is None:
        return None
    r = str(getattr(raw, "name", raw)).lower()
    if r in ("tool_calls", "tool_use", "function_call"):
        return "tool_calls"
    if r in ("length", "max_tokens", "max_output_tokens"):
        return "length"
    if r in ("content_filter", "safety", "refusal", "recitation", "blocklist"):
        return "content_filter"
    return "stop"


def validate_conversation(messages: list) -> None:
    """Reject malformed conversations instead of letting adapters silently
    rewrite them: roles must be known, there must be a non-system message,
    and the first and last non-system messages must be from the user."""
    if not messages:
        raise ValueError("messages must not be empty")
    for m in messages:
        if m.role not in VALID_ROLES:
            raise ValueError(
                f"Invalid message role '{m.role}'; expected one of {VALID_ROLES}"
            )
    rest = [m for m in messages if m.role != "system"]
    if not rest:
        raise ValueError("messages must contain at least one non-system message")
    if rest[0].role != "user":
        raise ValueError("The first non-system message must have role 'user'")
    if rest[-1].role not in ("user", "tool"):
        raise ValueError("The last message must have role 'user' or 'tool'")
    for m in rest:
        if m.role == "tool" and not m.tool_call_id:
            raise ValueError("A 'tool' message needs a tool_call_id")


def split_system(request: "CompletionRequest") -> tuple[Optional[str], list[Message]]:
    """Merge ``system_prompt`` and system-role messages (in order) into one
    system string; return it with the remaining non-system messages."""
    parts = [request.system_prompt] if request.system_prompt else []
    rest: list[Message] = []
    for m in request.messages:
        if m.role == "system":
            if m.content:
                parts.append(m.content)
        else:
            rest.append(m)
    return ("\n\n".join(parts) or None), rest


IMAGE_TYPES = ("image/jpeg", "image/png", "image/gif", "image/webp")
_DATA_URL = re.compile(r"^data:([\w.+-]+/[\w.+-]+);base64,(.+)$", re.DOTALL)


def parse_data_url(url: str) -> Optional[dict]:
    """``{"media_type", "data"}`` for a base64 data URL, else None."""
    m = _DATA_URL.match(url or "")
    return {"media_type": m.group(1).lower(), "data": m.group(2)} if m else None


def image_url(image: dict) -> str:
    """The image as a URL an OpenAI-style endpoint accepts."""
    if image.get("url"):
        return image["url"]
    return f"data:{image['media_type']};base64,{image['data']}"


def request_images(request: "CompletionRequest") -> list[dict]:
    return [img for m in request.messages for img in (m.images or [])]


def parse_arguments(raw) -> dict:
    """Tool-call arguments (a JSON string) as a dict; {} when empty or invalid."""
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def tool_specs(request: "CompletionRequest") -> list[dict]:
    """Flat ``{name, description, parameters}`` for each requested tool."""
    specs = []
    for t in request.tools or []:
        fn = t.get("function") or {}
        specs.append(
            {
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "parameters": fn.get("parameters")
                or {"type": "object", "properties": {}},
            }
        )
    return specs


def forced_tool_name(tool_choice) -> Optional[str]:
    if isinstance(tool_choice, dict):
        return (tool_choice.get("function") or {}).get("name")
    return None


def json_instruction(response_format: Optional[dict]) -> Optional[str]:
    """System text that asks a model without native JSON mode for JSON."""
    if not response_format:
        return None
    kind = response_format.get("type")
    if kind not in ("json_object", "json_schema"):
        return None
    text = "Respond with a single valid JSON value and nothing else: no prose and no code fences."
    if kind == "json_schema":
        schema = (response_format.get("json_schema") or {}).get("schema")
        if schema:
            text += " It must conform to this JSON schema: " + json.dumps(
                schema, separators=(",", ":")
            )
    return text


def strip_json_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        t = t.rsplit("```", 1)[0]
    return t.strip()


def system_with_json(request: "CompletionRequest") -> Optional[str]:
    """``split_system`` text plus a JSON instruction when one is needed."""
    system = split_system(request)[0]
    extra = json_instruction(request.response_format)
    return "\n\n".join(p for p in (system, extra) if p) or None


def openai_messages(
    request: "CompletionRequest", extra_system: Optional[str] = None
) -> list[dict]:
    """Messages in OpenAI's wire format, including tool calls and results."""
    system, rest = split_system(request)
    system = "\n\n".join(p for p in (system, extra_system) if p) or None
    out: list[dict] = [{"role": "system", "content": system}] if system else []
    for m in rest:
        if m.role == "tool":
            out.append(
                {"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content}
            )
        elif m.role == "assistant" and m.tool_calls:
            out.append(
                {
                    "role": "assistant",
                    "content": m.content or None,
                    "tool_calls": [
                        {
                            "id": c["id"],
                            "type": "function",
                            "function": {
                                "name": c["name"],
                                "arguments": c.get("arguments") or "{}",
                            },
                        }
                        for c in m.tool_calls
                    ],
                }
            )
        elif m.images:
            parts: list[dict] = (
                [{"type": "text", "text": m.content}] if m.content else []
            )
            parts.extend(
                {"type": "image_url", "image_url": {"url": image_url(i)}}
                for i in m.images
            )
            out.append({"role": m.role, "content": parts})
        else:
            out.append({"role": m.role, "content": m.content})
    return out


def openai_tool_kwargs(
    request: "CompletionRequest", native_schema: bool = True
) -> dict:
    """``tools``, ``tool_choice`` and ``response_format`` kwargs for an
    OpenAI-compatible endpoint. With ``native_schema`` False a json_schema
    format is downgraded to json_object (the schema goes in the prompt)."""
    kw: dict = {}
    if request.tools:
        kw["tools"] = request.tools
        if request.tool_choice is not None:
            kw["tool_choice"] = request.tool_choice
    rf = request.response_format
    if rf and rf.get("type") in ("json_object", "json_schema"):
        kw["response_format"] = (
            rf
            if (native_schema or rf["type"] == "json_object")
            else {"type": "json_object"}
        )
    return kw


def openai_extract_tool_calls(message) -> list[dict]:
    calls = []
    for c in getattr(message, "tool_calls", None) or []:
        fn = getattr(c, "function", None)
        calls.append(
            {
                "id": getattr(c, "id", None) or "",
                "name": getattr(fn, "name", "") or "",
                "arguments": getattr(fn, "arguments", None) or "{}",
            }
        )
    return calls


# Backstop timeout for the underlying HTTP client, in seconds. The router
# enforces the real per-provider timeouts (coreai.router) and cancels slow
# calls first; this only guarantees a hung connection is eventually closed.
# SDK-internal retries are disabled everywhere (max_retries=0) because the
# router owns retry and failover. Left on, they stack under the router's own
# retries and hide failures such as HTTP 529 from the circuit breaker.
SDK_TIMEOUT_S = 150.0


class BaseProvider(ABC):
    name: str = "base"
    default_model: str = ""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.total_requests = 0
        self.total_tokens = 0
        self.total_cost = 0.0
        logger.info(f"Initialized provider: {self.name}")

    @abstractmethod
    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        pass

    supports_tools: bool = False
    supports_vision: bool = False
    supports_embeddings: bool = False
    default_embedding_model: str = ""

    async def embed(self, texts: list[str], model: Optional[str] = None):
        """Return ``(vectors, input_tokens, model)``. Providers that offer
        embeddings override this and set ``supports_embeddings``."""
        raise NotImplementedError(f"{self.name} does not support embeddings")

    @abstractmethod
    def stream(self, request: CompletionRequest) -> AsyncGenerator[str, None]:
        """Async generator of text chunks (implement as ``async def`` with ``yield``)."""
        raise NotImplementedError

    def estimate_cost(self, input_tokens: int, output_tokens: int, model: str) -> float:
        """Estimated USD for a call (routing, pre-flight checks). Does not count
        toward the unpriced-model metric; use ``_bill`` for a real response."""
        from coreai.pricing import get_pricing

        return get_pricing().cost(
            self.name, model, input_tokens, output_tokens, track=False
        )

    def _bill(self, input_tokens: int, output_tokens: int, model: str) -> float:
        """USD for a completed call. An unlisted model is counted and logged."""
        from coreai.pricing import get_pricing

        return get_pricing().cost(self.name, model, input_tokens, output_tokens)

    @abstractmethod
    def count_tokens(self, text: str, model: str) -> int:
        pass

    def _track(self, response: CompletionResponse):
        self.total_requests += 1
        self.total_tokens += response.input_tokens + response.output_tokens
        self.total_cost += response.cost_usd

    def stats(self) -> dict:
        return {
            "provider": self.name,
            "total_requests": self.total_requests,
            "total_tokens": self.total_tokens,
            "total_cost_usd": round(self.total_cost, 6),
        }
