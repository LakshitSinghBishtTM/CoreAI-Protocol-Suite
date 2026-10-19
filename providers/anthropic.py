import time
from typing import AsyncGenerator

import anthropic as sdk
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


# NOTE: ``request.temperature`` is intentionally not forwarded. The current
# Anthropic SDK's messages API does not accept a ``temperature`` argument, so
# passing it raised a TypeError on every call (found by an end-to-end test
# against SDK 1.14). Other providers still receive it.


class AnthropicProvider(BaseProvider):
    name = "anthropic"
    default_model = "claude-haiku-4-5"

    def __init__(self, api_key: str):
        super().__init__(api_key)
        self.client = sdk.AsyncAnthropic(
            api_key=api_key, timeout=SDK_TIMEOUT_S, max_retries=0
        )

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        model = request.model or self.default_model
        system = split_system(request)[0]
        messages = self._build_messages(request)

        start = time.monotonic()
        try:
            response = await self.client.messages.create(
                model=model,
                max_tokens=request.max_tokens,
                system=system or sdk.NOT_GIVEN,
                messages=messages,
            )
        except Exception as e:
            logger.error(f"[anthropic] completion failed: {e}")
            raise

        latency_ms = (time.monotonic() - start) * 1000
        input_tokens = response.usage.input_tokens
        output_tokens = response.usage.output_tokens
        cost = self._bill(input_tokens, output_tokens, model)
        content = "".join(
            b.text
            for b in (response.content or [])
            if isinstance(getattr(b, "text", None), str)
        )
        if not content:
            raise ValueError(
                f"[anthropic] response had no text content "
                f"(stop_reason={getattr(response, 'stop_reason', None)})"
            )

        result = CompletionResponse(
            content=content,
            model=model,
            provider=self.name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            latency_ms=latency_ms,
            metadata={"finish_reason": normalize_finish_reason(getattr(response, "stop_reason", None))},
        )
        self._track(result)
        logger.debug(
            f"[anthropic] {model} | {input_tokens}in {output_tokens}out | ${cost:.6f} | {latency_ms:.0f}ms"
        )
        return result

    async def stream(self, request: CompletionRequest) -> AsyncGenerator[str, None]:
        model = request.model or self.default_model
        system = split_system(request)[0]
        messages = self._build_messages(request)

        try:
            async with self.client.messages.stream(
                model=model,
                max_tokens=request.max_tokens,
                system=system or sdk.NOT_GIVEN,
                messages=messages,
            ) as stream:
                async for text in stream.text_stream:
                    yield text
                if request.stream_usage is not None:
                    try:
                        final = await stream.get_final_message()
                        request.stream_usage.report(
                            final.usage.input_tokens,
                            final.usage.output_tokens,
                            final.model,
                        )
                        request.stream_usage.finish_reason = normalize_finish_reason(
                            getattr(final, "stop_reason", None)
                        )
                    except Exception as exc:  # usage is best effort
                        logger.debug(f"[anthropic] no stream usage: {exc}")
        except Exception as e:
            logger.error(f"[anthropic] stream failed: {e}")
            raise

    def count_tokens(self, text: str, model: str) -> int:
        # Anthropic uses ~4 chars per token on average
        return len(text) // 4

    def _build_messages(self, request: CompletionRequest) -> list[dict]:
        # System text is sent via ``system=`` (see split_system); none in messages.
        rest = split_system(request)[1]
        messages = [{"role": m.role, "content": m.content} for m in rest]

        # Anthropic requires the first message to be from the user. Reject
        # rather than inventing a prompt turn.
        validate_conversation(request.messages)

        return messages
