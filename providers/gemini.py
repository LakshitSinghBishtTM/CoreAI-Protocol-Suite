"""
providers/gemini.py
"""

import time
from typing import AsyncGenerator

from google import genai
from google.genai import types
from loguru import logger

from .base import (
    normalize_finish_reason,
    validate_conversation,
    SDK_TIMEOUT_S,
    BaseProvider,
    CompletionRequest,
    CompletionResponse,
    split_system,
)


class GeminiProvider(BaseProvider):
    name = "gemini"
    default_model = "gemini-2.0-flash"

    def __init__(self, api_key: str):
        super().__init__(api_key)
        self.client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=int(SDK_TIMEOUT_S * 1000)),
        )

    def _build_config(self, request: CompletionRequest) -> types.GenerateContentConfig:
        return types.GenerateContentConfig(
            max_output_tokens=request.max_tokens,
            temperature=request.temperature,
            system_instruction=split_system(request)[0],
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
        cost = self._bill(input_tokens, output_tokens, model)
        content = response.text

        result = CompletionResponse(
            content=content,
            model=model,
            provider=self.name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            latency_ms=latency_ms,
            metadata={"finish_reason": self._finish(response)},
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
                meta = getattr(chunk, "usage_metadata", None)
                if meta is not None and request.stream_usage is not None:
                    out = getattr(meta, "candidates_token_count", None)
                    thoughts = getattr(meta, "thoughts_token_count", None)
                    if isinstance(out, int) and isinstance(thoughts, int):
                        out += thoughts  # thinking tokens are billed as output
                    request.stream_usage.report(
                        getattr(meta, "prompt_token_count", None), out
                    )
                fr = self._finish(chunk)
                if fr is not None and request.stream_usage is not None:
                    request.stream_usage.finish_reason = fr
                if chunk.text:
                    yield chunk.text
        except Exception as e:
            logger.error(f"[gemini] stream failed: {e}")
            raise

    def count_tokens(self, text: str, model: str) -> int:
        return len(text) // 4

    def _build_messages(self, request: CompletionRequest) -> tuple[list[dict], str]:
        # Gemini uses history + last message separately
        # roles are "user" and "model" (not "assistant")
        validate_conversation(request.messages)
        history = []
        messages = split_system(request)[1]

        for msg in messages[:-1]:
            role = "model" if msg.role == "assistant" else "user"
            history.append({"role": role, "parts": [{"text": msg.content}]})

        # validate_conversation guarantees the last message is a user turn.
        return history, messages[-1].content

    @staticmethod
    def _finish(resp):
        try:
            cands = getattr(resp, "candidates", None) or []
            return normalize_finish_reason(cands[0].finish_reason) if cands else None
        except Exception:
            return None
