"""
providers/gemini.py
"""

import time
from typing import AsyncGenerator

from google import genai
from google.genai import types
from loguru import logger

from .base import BaseProvider, CompletionRequest, CompletionResponse

GEMINI_PRICING = {
    "gemini-1.5-pro": {"input": 0.0000035, "output": 0.0000105},
    "gemini-1.5-flash": {"input": 0.000000075, "output": 0.0000003},
    "gemini-1.5-flash-8b": {"input": 0.0000000375, "output": 0.00000015},
    "gemini-2.0-flash": {"input": 0.0000001, "output": 0.0000004},
    "gemini-2.5-pro": {"input": 0.0000125, "output": 0.00001},
}


class GeminiProvider(BaseProvider):
    name = "gemini"
    default_model = "gemini-2.0-flash"

    def __init__(self, api_key: str):
        super().__init__(api_key)
        self.client = genai.Client(api_key=api_key)

    def _build_config(self, request: CompletionRequest) -> types.GenerateContentConfig:
        return types.GenerateContentConfig(
            max_output_tokens=request.max_tokens,
            temperature=request.temperature,
            system_instruction=request.system_prompt or None,
        )

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        model = request.model or self.default_model
        history, last_message = self._build_messages(request)
        config = self._build_config(request)

        start = time.monotonic()
        try:
            chat = self.client.aio.chats.create(
                model=model, config=config, history=history
            )
            response = await chat.send_message(last_message)
        except Exception as e:
            logger.error(f"[gemini] completion failed: {e}")
            raise

        latency_ms = (time.monotonic() - start) * 1000
        input_tokens = response.usage_metadata.prompt_token_count
        output_tokens = response.usage_metadata.candidates_token_count
        cost = self.estimate_cost(input_tokens, output_tokens, model)
        content = response.text

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
            f"[gemini] {model} | {input_tokens}in {output_tokens}out | ${cost:.6f} | {latency_ms:.0f}ms"
        )
        return result

    async def stream(self, request: CompletionRequest) -> AsyncGenerator[str, None]:
        model = request.model or self.default_model
        history, last_message = self._build_messages(request)
        config = self._build_config(request)

        try:
            chat = self.client.aio.chats.create(
                model=model, config=config, history=history
            )
            stream = await chat.send_message_stream(last_message)
            async for chunk in stream:
                if chunk.text:
                    yield chunk.text
        except Exception as e:
            logger.error(f"[gemini] stream failed: {e}")
            raise

    def estimate_cost(self, input_tokens: int, output_tokens: int, model: str) -> float:
        pricing = GEMINI_PRICING.get(model, {"input": 0.0000001, "output": 0.0000004})
        return (input_tokens * pricing["input"]) + (output_tokens * pricing["output"])

    def count_tokens(self, text: str, model: str) -> int:
        return len(text) // 4

    def _build_messages(self, request: CompletionRequest) -> tuple[list[dict], str]:
        # Gemini uses history + last message separately
        # roles are "user" and "model" (not "assistant")
        history = []
        messages = [m for m in request.messages if m.role != "system"]

        for msg in messages[:-1]:
            role = "model" if msg.role == "assistant" else "user"
            history.append({"role": role, "parts": [{"text": msg.content}]})

        last_message = messages[-1].content if messages else "Hello"
        return history, last_message
