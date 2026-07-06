import time
from typing import AsyncGenerator

import anthropic as sdk
from loguru import logger

from .base import BaseProvider, CompletionRequest, CompletionResponse

ANTHROPIC_PRICING = {
    "claude-opus-4-5": {"input": 0.000015, "output": 0.000075},
    "claude-sonnet-4-5": {"input": 0.000003, "output": 0.000015},
    "claude-haiku-4-5": {"input": 0.0000008, "output": 0.000004},
    "claude-opus-4-0": {"input": 0.000015, "output": 0.000075},
    "claude-sonnet-4-0": {"input": 0.000003, "output": 0.000015},
}


class AnthropicProvider(BaseProvider):
    name = "anthropic"
    default_model = "claude-haiku-4-5"

    def __init__(self, api_key: str):
        super().__init__(api_key)
        self.client = sdk.AsyncAnthropic(api_key=api_key)

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        model = request.model or self.default_model
        messages = self._build_messages(request)

        start = time.monotonic()
        try:
            response = await self.client.messages.create(
                model=model,
                max_tokens=request.max_tokens,
                temperature=request.temperature,
                system=request.system_prompt or sdk.NOT_GIVEN,
                messages=messages,
            )
        except Exception as e:
            logger.error(f"[anthropic] completion failed: {e}")
            raise

        latency_ms = (time.monotonic() - start) * 1000
        input_tokens = response.usage.input_tokens
        output_tokens = response.usage.output_tokens
        cost = self.estimate_cost(input_tokens, output_tokens, model)
        content = response.content[0].text

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
            f"[anthropic] {model} | {input_tokens}in {output_tokens}out | ${cost:.6f} | {latency_ms:.0f}ms"
        )
        return result

    async def stream(self, request: CompletionRequest) -> AsyncGenerator[str, None]:
        model = request.model or self.default_model
        messages = self._build_messages(request)

        try:
            async with self.client.messages.stream(
                model=model,
                max_tokens=request.max_tokens,
                temperature=request.temperature,
                system=request.system_prompt or sdk.NOT_GIVEN,
                messages=messages,
            ) as stream:
                async for text in stream.text_stream:
                    yield text
        except Exception as e:
            logger.error(f"[anthropic] stream failed: {e}")
            raise

    def estimate_cost(self, input_tokens: int, output_tokens: int, model: str) -> float:
        pricing = ANTHROPIC_PRICING.get(model, {"input": 0.000003, "output": 0.000015})
        return (input_tokens * pricing["input"]) + (output_tokens * pricing["output"])

    def count_tokens(self, text: str, model: str) -> int:
        # Anthropic uses ~4 chars per token on average
        return len(text) // 4

    def _build_messages(self, request: CompletionRequest) -> list[dict]:
        messages = []
        for msg in request.messages:
            # Anthropic doesn't allow system role in messages array
            if msg.role == "system":
                continue
            messages.append({"role": msg.role, "content": msg.content})

        # Anthropic requires first message to be from user
        if not messages or messages[0]["role"] != "user":
            messages.insert(0, {"role": "user", "content": "Hello"})

        return messages
