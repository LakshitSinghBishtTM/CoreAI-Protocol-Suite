"""
CoreAI Protocol Suite - DeepSeek Provider
DeepSeek uses an OpenAI-compatible API, so we use the openai SDK
pointed at DeepSeek's base URL.
"""

import time
from typing import AsyncGenerator

from openai import AsyncOpenAI
from loguru import logger

from .base import BaseProvider, CompletionRequest, CompletionResponse

DEEPSEEK_PRICING = {
    "deepseek-chat":     {"input": 0.00000027, "output": 0.0000011},
    "deepseek-reasoner": {"input": 0.00000055, "output": 0.00000219},
}

DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"


class DeepSeekProvider(BaseProvider):
    name = "deepseek"
    default_model = "deepseek-chat"

    def __init__(self, api_key: str):
        super().__init__(api_key)
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=DEEPSEEK_BASE_URL,
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
                stream=False,
            )
        except Exception as e:
            logger.error(f"[deepseek] completion failed: {e}")
            raise

        latency_ms = (time.monotonic() - start) * 1000
        input_tokens = response.usage.prompt_tokens
        output_tokens = response.usage.completion_tokens
        cost = self.estimate_cost(input_tokens, output_tokens, model)
        content = response.choices[0].message.content or ""

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
            f"[deepseek] {model} | {input_tokens}in {output_tokens}out | "
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
            )
            async for chunk in stream:
                delta = chunk.choices[0].delta.content
                if delta:
                    yield delta
        except Exception as e:
            logger.error(f"[deepseek] stream failed: {e}")
            raise

    def estimate_cost(self, input_tokens: int, output_tokens: int, model: str) -> float:
        pricing = DEEPSEEK_PRICING.get(model, DEEPSEEK_PRICING["deepseek-chat"])
        return (input_tokens * pricing["input"]) + (output_tokens * pricing["output"])

    def count_tokens(self, text: str, model: str) -> int:
        # DeepSeek tokenizer is close to GPT — ~4 chars per token
        return max(1, len(text) // 4)

    def _build_messages(self, request: CompletionRequest) -> list[dict]:
        messages = []
        for msg in request.messages:
            messages.append({"role": msg.role, "content": msg.content})

        # Inject system_prompt if set separately and not already in messages
        has_system = any(m["role"] == "system" for m in messages)
        if request.system_prompt and not has_system:
            messages.insert(0, {"role": "system", "content": request.system_prompt})

        return messages
