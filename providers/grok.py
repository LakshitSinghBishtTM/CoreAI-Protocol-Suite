"""
providers/grok.py

Grok uses an OpenAI-compatible API, just a different base URL and models.
"""

import importlib
import time
from typing import AsyncGenerator

from loguru import logger

# importlib guarantees we get the top-level 'openai' SDK package, not the
# providers.openai submodule which shares the same short name.
AsyncOpenAI = importlib.import_module("openai").AsyncOpenAI

from .base import SDK_TIMEOUT_S, BaseProvider, CompletionRequest, CompletionResponse, split_system


class GrokProvider(BaseProvider):
    name = "grok"
    default_model = "grok-3-mini"
    base_url = "https://api.x.ai/v1"

    def __init__(self, api_key: str):
        super().__init__(api_key)
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=self.base_url,
            timeout=SDK_TIMEOUT_S,
            max_retries=0,
        )

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        model = request.model or self.default_model
        messages = self._build_messages(request)

        start = time.monotonic()
        try:
            response = await self.client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=request.max_tokens,
                temperature=request.temperature,
            )
        except Exception as e:
            logger.error(f"[grok] completion failed: {e}")
            raise

        latency_ms = (time.monotonic() - start) * 1000
        input_tokens = response.usage.prompt_tokens
        output_tokens = response.usage.completion_tokens
        cost = self._bill(input_tokens, output_tokens, model)
        content = response.choices[0].message.content

        result = CompletionResponse(
            content=content,
            model=model,
            provider=self.name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            latency_ms=latency_ms,
        )
        self._track(result)
        logger.debug(
            f"[grok] {model} | {input_tokens}in {output_tokens}out | "
            f"${cost:.6f} | {latency_ms:.0f}ms"
        )
        return result

    async def stream(self, request: CompletionRequest) -> AsyncGenerator[str, None]:
        model = request.model or self.default_model
        messages = self._build_messages(request)

        try:
            stream = await self.client.chat.completions.create(
                model=model,
                messages=messages,
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
                        getattr(chunk, "model", None),
                    )
                if not chunk.choices:  # the final usage-only chunk has none
                    continue
                delta = chunk.choices[0].delta.content
                if delta:
                    yield delta
        except Exception as e:
            logger.error(f"[grok] stream failed: {e}")
            raise

    def count_tokens(self, text: str, model: str) -> int:
        return len(text) // 4

    def _build_messages(self, request: CompletionRequest) -> list[dict]:
        system, rest = split_system(request)
        messages = [{"role": "system", "content": system}] if system else []
        messages.extend({"role": m.role, "content": m.content} for m in rest)
        return messages
