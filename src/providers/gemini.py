"""
providers/gemini.py
"""

import base64
import json
import time
from typing import Any, AsyncGenerator

from google import genai
from google.genai import types
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
    system_with_json,
    tool_specs,
    validate_conversation,
)


class GeminiProvider(BaseProvider):
    name = "gemini"
    default_model = "gemini-2.0-flash"
    supports_tools = True
    supports_vision = True

    def __init__(self, api_key: str):
        super().__init__(api_key)
        self.client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=int(SDK_TIMEOUT_S * 1000)),
        )

    def _build_config(self, request: CompletionRequest) -> types.GenerateContentConfig:
        kw: dict = {
            "max_output_tokens": request.max_tokens,
            "temperature": request.temperature,
            "system_instruction": system_with_json(request),
        }
        if (request.response_format or {}).get("type") in (
            "json_object",
            "json_schema",
        ):
            kw["response_mime_type"] = "application/json"
        choice = request.tool_choice
        if request.tools and choice != "none":
            field = (
                "parameters_json_schema"
                if "parameters_json_schema" in types.FunctionDeclaration.model_fields
                else "parameters"
            )
            kw["tools"] = [
                types.Tool(
                    function_declarations=[
                        types.FunctionDeclaration(
                            name=t["name"],
                            description=t["description"],
                            **{field: t["parameters"]},
                        )
                        for t in tool_specs(request)
                    ]
                )
            ]
            forced = forced_tool_name(choice)
            mode = "ANY" if (forced or choice == "required") else "AUTO"
            kw["tool_config"] = types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(
                    mode=mode, allowed_function_names=[forced] if forced else None
                )
            )
        return types.GenerateContentConfig(**kw)

    @staticmethod
    def _tool_calls(response) -> list[dict]:
        calls = []
        try:
            parts = response.candidates[0].content.parts or []
            for part in parts:
                fc = getattr(part, "function_call", None)
                if fc is not None and getattr(fc, "name", None):
                    calls.append(
                        {
                            "id": f"call_{len(calls)}",
                            "name": fc.name,
                            "arguments": json.dumps(
                                dict(getattr(fc, "args", None) or {})
                            ),
                        }
                    )
        except Exception:
            return []
        return calls

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
        tool_calls = self._tool_calls(response)
        content = (response.text if not tool_calls else "") or ""

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

    def _build_messages(self, request: CompletionRequest) -> tuple[list[dict], Any]:
        # Gemini uses history + last message separately
        # roles are "user" and "model" (not "assistant")
        validate_conversation(request.messages)
        names: dict[str, str] = {}  # tool_call_id -> function name
        contents: list[dict] = []
        for msg in split_system(request)[1]:
            if msg.role == "tool":
                part = {
                    "function_response": {
                        "name": names.get(msg.tool_call_id or "", "tool"),
                        "response": {"result": msg.content},
                    }
                }
                last = contents[-1] if contents else None
                if (
                    last
                    and last["role"] == "user"
                    and "function_response" in last["parts"][-1]
                ):
                    last["parts"].append(part)  # results of one turn share a message
                else:
                    contents.append({"role": "user", "parts": [part]})
            elif msg.role == "assistant" and msg.tool_calls:
                parts = [{"text": msg.content}] if msg.content else []
                for c in msg.tool_calls:
                    names[c["id"]] = c["name"]
                    parts.append(
                        {
                            "function_call": {
                                "name": c["name"],
                                "args": parse_arguments(c.get("arguments")),
                            }
                        }
                    )
                contents.append({"role": "model", "parts": parts})
            else:
                role = "model" if msg.role == "assistant" else "user"
                parts = [{"inline_data": self._blob(i)} for i in msg.images or []]
                if msg.content or not parts:
                    parts.append({"text": msg.content})
                contents.append({"role": role, "parts": parts})

        # validate_conversation guarantees the last message is a user or tool turn.
        last = contents[-1]
        history = contents[:-1]
        if len(last["parts"]) == 1 and "text" in last["parts"][0]:
            return history, last["parts"][0]["text"]
        return history, last["parts"]

    @staticmethod
    def _blob(image: dict) -> dict:
        if not image.get("data"):
            raise ValueError(
                "Gemini accepts images as inline data only (send a data: URL, not a link)."
            )
        return {
            "mime_type": image["media_type"],
            "data": base64.b64decode(image["data"]),
        }

    def _metadata(self, response, tool_calls: list[dict]) -> dict:
        meta = {"finish_reason": self._finish(response)}
        if tool_calls:
            meta["tool_calls"] = tool_calls
            meta["finish_reason"] = "tool_calls"
        return meta

    @staticmethod
    def _finish(resp):
        try:
            cands = getattr(resp, "candidates", None) or []
            return normalize_finish_reason(cands[0].finish_reason) if cands else None
        except Exception:
            return None
