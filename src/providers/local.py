"""providers/local.py

A generic provider for any OpenAI-compatible server the user runs themselves:
LM Studio, Ollama, llama.cpp's ``llama-server``, vLLM. CaiPS routes to it; the
user runs the model.

Servers come from ``config/local.yml`` (or ``$COREAI_LOCAL_FILE``); see
``docs/local-models.md``. Each server becomes one provider named after its key.
"""

import importlib
import time
from dataclasses import dataclass, replace
from typing import Any, AsyncGenerator, Optional

import httpx
from loguru import logger

AsyncOpenAI = importlib.import_module("openai").AsyncOpenAI

from .base import (  # noqa: E402
    BaseProvider,
    CompletionRequest,
    CompletionResponse,
    json_instruction,
    normalize_finish_reason,
    openai_extract_tool_calls,
    openai_messages,
    openai_tool_kwargs,
    request_images,
)

PRESETS: dict[str, str] = {
    "lmstudio": "http://localhost:1234/v1",
    "ollama": "http://localhost:11434/v1",
    "llama-server": "http://localhost:8080/v1",
    "vllm": "http://localhost:8000/v1",
}

# Short connect timeout: desktop servers come and go, so a dead one should
# fail fast (and trip the circuit breaker) instead of hanging a request.
CONNECT_TIMEOUT_S = 2.0
READ_TIMEOUT_S = 300.0


@dataclass(frozen=True)
class Capabilities:
    """What one local model can do. Local models differ widely, so nothing is
    assumed: tools and vision are off unless configured."""

    tools: bool = False
    vision: bool = False
    json_mode: bool = True
    context_length: Optional[int] = None


class LocalProvider(BaseProvider):
    is_local = True
    supports_tools = True  # provider-level; per-model truth is in capability_problem()
    supports_vision = True
    supports_embeddings = True

    def __init__(
        self,
        name: str,
        base_url: str,
        api_key: str = "",
        models: Optional[list[str]] = None,
        capabilities: Optional[dict[str, Any]] = None,
        default_model: str = "",
        cost_per_hour_usd: float = 0.0,
    ):
        super().__init__(api_key or "not-needed")
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.models: set[str] = set(models or [])
        self.default_model = default_model or (
            sorted(self.models)[0] if self.models else ""
        )
        self.cost_per_hour_usd = float(cost_per_hour_usd or 0.0)
        self.up = True  # optimistic until a probe says otherwise
        caps = dict(capabilities or {})
        self._default_caps = _caps(caps.pop("default", {}), Capabilities())
        self._caps = {m: _caps(c, self._default_caps) for m, c in caps.items()}
        self.client = AsyncOpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=httpx.Timeout(READ_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
            max_retries=0,
        )

    # ---------------------------------------------------------- capabilities

    def capabilities(self, model: Optional[str]) -> Capabilities:
        return self._caps.get(model or self.default_model, self._default_caps)

    def capability_problem(self, request: CompletionRequest) -> Optional[str]:
        """Why this server's model can't serve ``request``, or None."""
        model = request.model or self.default_model
        if not self.up:
            return f"Local server '{self.name}' is down (failed its last health check)."
        cap = self.capabilities(model)
        if request.tools and not cap.tools:
            return f"Local model '{model}' on '{self.name}' is not configured for tool calling."
        if request_images(request) and not cap.vision:
            return (
                f"Local model '{model}' on '{self.name}' does not accept image input."
            )
        if cap.context_length:
            need = sum(len(m.content or "") for m in request.messages) // 4 + (
                request.max_tokens or 0
            )
            if need > cap.context_length:
                return (
                    f"Request (~{need} tokens) exceeds the {cap.context_length}-token "
                    f"context of local model '{model}'."
                )
        return None

    # ------------------------------------------------------------- discovery

    async def refresh_models(self) -> list[str]:
        """Ask the server which models are loaded (``GET /v1/models``).
        Never raises: a server that is down simply keeps its last known list."""
        try:
            async with httpx.AsyncClient(timeout=CONNECT_TIMEOUT_S + 3) as http:
                r = await http.get(
                    f"{self.base_url}/models",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
                r.raise_for_status()
                found = [m["id"] for m in r.json().get("data", []) if m.get("id")]
        except Exception as exc:
            logger.warning(f"[{self.name}] model discovery failed: {exc}")
            return sorted(self.models)
        self.models |= set(found)
        if not self.default_model and found:
            self.default_model = sorted(found)[0]
        logger.info(f"[{self.name}] discovered models: {', '.join(found) or 'none'}")
        return found

    async def probe(self) -> bool:
        """Health check: mark the server up or down from ``GET /v1/models``.
        On recovery, models are re-discovered. Never raises."""
        was_up = self.up
        try:
            async with httpx.AsyncClient(timeout=CONNECT_TIMEOUT_S + 1) as http:
                r = await http.get(
                    f"{self.base_url}/models",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
                r.raise_for_status()
                found = [m["id"] for m in r.json().get("data", []) if m.get("id")]
            self.up = True
            self.models |= set(found)
            if not self.default_model and found:
                self.default_model = sorted(found)[0]
        except Exception as exc:
            self.up = False
            if was_up:
                logger.warning(f"[{self.name}] marked down: {exc}")
            return False
        if not was_up:
            logger.info(f"[{self.name}] back up")
        return True

    # --------------------------------------------------------------- billing

    def estimate_cost(self, input_tokens: int, output_tokens: int, model: str) -> float:
        return 0.0

    def _bill(
        self, input_tokens: int, output_tokens: int, model: str, latency_s: float = 0.0
    ) -> float:
        """Zero API cost; optionally an hardware/electricity estimate per hour."""
        return round(self.cost_per_hour_usd * latency_s / 3600.0, 8)

    def count_tokens(self, text: str, model: str) -> int:
        return max(1, len(text) // 4)

    # ------------------------------------------------------------ completion

    def _build_messages(self, request: CompletionRequest) -> list[dict]:
        return openai_messages(request, json_instruction(request.response_format))

    def _need_model(self, request: CompletionRequest) -> str:
        model = request.model or self.default_model
        if not model:
            raise ValueError(
                f"No model given and none known for local server '{self.name}'."
            )
        return model

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        model = self._need_model(request)
        start = time.monotonic()
        try:
            response = await self.client.chat.completions.create(
                model=model,
                messages=self._build_messages(request),
                **openai_tool_kwargs(request, native_schema=False),
                max_tokens=request.max_tokens,
                temperature=request.temperature,
                stream=False,
            )
        except Exception as e:
            logger.error(f"[{self.name}] completion failed: {e}")
            raise
        elapsed = time.monotonic() - start
        choice = response.choices[0]
        content = choice.message.content or ""
        usage = getattr(response, "usage", None)
        in_tok = getattr(usage, "prompt_tokens", None)
        out_tok = getattr(usage, "completion_tokens", None)
        if in_tok is None:
            in_tok = sum(
                self.count_tokens(m.content or "", model) for m in request.messages
            )
        if out_tok is None:
            out_tok = self.count_tokens(content, model)
        meta: dict = {
            "finish_reason": normalize_finish_reason(choice.finish_reason),
            "local": True,
        }
        calls = openai_extract_tool_calls(choice.message)
        if calls:
            meta["tool_calls"] = calls
            meta["finish_reason"] = "tool_calls"
        result = CompletionResponse(
            content=content,
            model=model,
            provider=self.name,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_usd=self._bill(in_tok, out_tok, model, elapsed),
            latency_ms=elapsed * 1000,
            metadata=meta,
        )
        self._track(result)
        return result

    async def stream(self, request: CompletionRequest) -> AsyncGenerator[str, None]:
        model = self._need_model(request)
        start = time.monotonic()
        try:
            stream = await self.client.chat.completions.create(
                model=model,
                messages=self._build_messages(request),
                **openai_tool_kwargs(request, native_schema=False),
                max_tokens=request.max_tokens,
                temperature=request.temperature,
                stream=True,
                stream_options={"include_usage": True},
            )
            async for chunk in stream:
                usage = getattr(chunk, "usage", None)
                if usage is not None and request.stream_usage is not None:
                    request.stream_usage.report(
                        getattr(usage, "prompt_tokens", None),
                        getattr(usage, "completion_tokens", None),
                        getattr(chunk, "model", None) or model,
                    )
                if not chunk.choices:
                    continue
                fr = getattr(chunk.choices[0], "finish_reason", None)
                if fr and request.stream_usage is not None:
                    request.stream_usage.finish_reason = normalize_finish_reason(fr)
                delta = chunk.choices[0].delta.content
                if delta:
                    yield delta
        except Exception as e:
            logger.error(f"[{self.name}] stream failed: {e}")
            raise
        finally:
            logger.debug(
                f"[{self.name}] stream {model} {time.monotonic() - start:.2f}s"
            )

    async def embed(self, texts: list[str], model: Optional[str] = None):
        model = model or self.default_model
        r = await self.client.embeddings.create(model=model, input=texts)
        vectors = [d.embedding for d in r.data]
        tokens = getattr(getattr(r, "usage", None), "prompt_tokens", None) or sum(
            self.count_tokens(t, model) for t in texts
        )
        return vectors, tokens, model


def _caps(raw: Any, base: Capabilities) -> Capabilities:
    if not isinstance(raw, dict):
        return base
    allowed = {k: v for k, v in raw.items() if k in Capabilities.__dataclass_fields__}
    return replace(base, **allowed)


def build_local_providers(config: dict) -> dict[str, LocalProvider]:
    """Build providers from a parsed ``local.yml``: ``{"servers": {name: {...}}}``."""
    out: dict[str, LocalProvider] = {}
    for name, cfg in (config.get("servers") or {}).items():
        cfg = cfg or {}
        url = cfg.get("base_url") or PRESETS.get(cfg.get("preset", name))
        if not url:
            raise ValueError(
                f"local server '{name}': set base_url or a preset ({', '.join(PRESETS)})"
            )
        import os

        key = (
            os.getenv(cfg["api_key_env"], "")
            if cfg.get("api_key_env")
            else cfg.get("api_key", "")
        )
        out[name] = LocalProvider(
            name=name,
            base_url=url,
            api_key=key,
            models=cfg.get("models"),
            capabilities=cfg.get("capabilities"),
            default_model=cfg.get("default_model", ""),
            cost_per_hour_usd=cfg.get("cost_per_hour_usd", 0.0),
        )
    return out


async def monitor_local(providers: dict, interval_s: float = 10.0) -> None:
    """Probe every local server forever; cancel the task to stop."""
    import asyncio

    local = [p for p in providers.values() if getattr(p, "is_local", False)]
    while local:
        await asyncio.gather(*(p.probe() for p in local))
        await asyncio.sleep(interval_s)
