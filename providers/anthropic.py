import json
import time
from typing import AsyncGenerator

import anthropic as sdk
from loguru import logger

from .base import (
    SDK_TIMEOUT_S,
    BaseProvider,
    CompletionRequest,
    CompletionResponse,
    forced_tool_name,
    normalize_finish_reason,
    parse_arguments,
    split_system,
    strip_json_fences,
    system_with_json,
    tool_specs,
    validate_conversation,
)

# NOTE: ``request.temperature`` is intentionally not forwarded. The current
# Anthropic SDK's messages API does not accept a ``temperature`` argument, so
# passing it raised a TypeError on every call (found by an end-to-end test
# against SDK 1.14). Other providers still receive it.


class AnthropicProvider(BaseProvider):
    name = "anthropic"
    default_model = "claude-haiku-4-5"
    supports_tools = True
    supports_vision = True

    def __init__(self, api_key: str):
        super().__init__(api_key)
        self.client = sdk.AsyncAnthropic(
            api_key=api_key, timeout=SDK_TIMEOUT_S, max_retries=0
        )

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        model = request.model or self.default_model
        system = system_with_json(request)
        messages = self._build_messages(request)

        start = time.monotonic()
        try:
            response = await self.client.messages.create(
                model=model,
                max_tokens=request.max_tokens,
                system=system or sdk.NOT_GIVEN,
                messages=messages,
                **self._tool_kwargs(request),
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
        tool_calls = [
            {"id": b.id, "name": b.name, "arguments": json.dumps(b.input or {})}
            for b in (response.content or [])
            if getattr(b, "type", None) == "tool_use"
        ]
        if request.response_format and not tool_calls:
            content = strip_json_fences(content)
        if not content and not tool_calls:
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
            metadata=self._metadata(response, tool_calls),
        )
        self._track(result)
        logger.debug(
            f"[anthropic] {model} | {input_tokens}in {output_tokens}out | ${cost:.6f} | {latency_ms:.0f}ms"
        )
        return result

    async def stream(self, request: CompletionRequest) -> AsyncGenerator[str, None]:
        model = request.model or self.default_model
        system = system_with_json(request)
        messages = self._build_messages(request)

        try:
            async with self.client.messages.stream(
                model=model,
                max_tokens=request.max_tokens,
                system=system or sdk.NOT_GIVEN,
                messages=messages,
                **self._tool_kwargs(request),
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

    @staticmethod
    def _metadata(response, tool_calls: list[dict]) -> dict:
        meta = {"finish_reason": normalize_finish_reason(getattr(response, "stop_reason", None))}
        if tool_calls:
            meta["tool_calls"] = tool_calls
            meta["finish_reason"] = "tool_calls"
        return meta

    @staticmethod
    def _tool_kwargs(request: CompletionRequest) -> dict:
        """OpenAI-style tools and tool_choice in Anthropic's shape."""
        choice = request.tool_choice
        if not request.tools or choice == "none":
            return {}
        kw: dict = {
            "tools": [
                {"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
                for t in tool_specs(request)
            ]
        }
        forced = forced_tool_name(choice)
        if forced:
            kw["tool_choice"] = {"type": "tool", "name": forced}
        elif choice == "required":
            kw["tool_choice"] = {"type": "any"}
        elif choice == "auto":
            kw["tool_choice"] = {"type": "auto"}
        return kw

    def _build_messages(self, request: CompletionRequest) -> list[dict]:
        # System text is sent via ``system=`` (see split_system); none in messages.
        validate_conversation(request.messages)
        messages: list[dict] = []
        for m in split_system(request)[1]:
            if m.role == "tool":
                block = {"type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.content}
                last = messages[-1] if messages else None
                if last and last["role"] == "user" and isinstance(last["content"], list):
                    last["content"].append(block)  # results of one turn share a message
                else:
                    messages.append({"role": "user", "content": [block]})
            elif m.role == "assistant" and m.tool_calls:
                blocks = [{"type": "text", "text": m.content}] if m.content else []
                blocks.extend(
                    {
                        "type": "tool_use",
                        "id": c["id"],
                        "name": c["name"],
                        "input": parse_arguments(c.get("arguments")),
                    }
                    for c in m.tool_calls
                )
                messages.append({"role": "assistant", "content": blocks})
            elif m.images:
                blocks = [self._image_block(i) for i in m.images]
                if m.content:
                    blocks.append({"type": "text", "text": m.content})
                messages.append({"role": m.role, "content": blocks})
            else:
                messages.append({"role": m.role, "content": m.content})
        return messages

    @staticmethod
    def _image_block(image: dict) -> dict:
        if image.get("url"):
            return {"type": "image", "source": {"type": "url", "url": image["url"]}}
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": image["media_type"], "data": image["data"]},
        }
