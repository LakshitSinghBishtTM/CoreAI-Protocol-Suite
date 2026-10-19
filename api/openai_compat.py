"""OpenAI-compatible endpoints: ``/v1/chat/completions`` and ``/v1/models``.

Point an OpenAI SDK at ``<host>/v1`` and pass a CaiPS token as the API key.

Supported: text messages (system, developer, user, assistant), ``model``,
``max_tokens`` / ``max_completion_tokens``, ``temperature``, ``stream`` and
``stream_options.include_usage``. Accepted and ignored: ``top_p``, ``seed``,
``user``, ``presence_penalty``, ``frequency_penalty``, ``logit_bias``.
Anything else that would change the answer (tools, ``response_format``,
``stop``, ``n`` above 1, image parts) is refused with a 400 rather than
silently dropped. Extension field: ``provider`` pins a provider.

Errors use OpenAI's envelope: ``{"error": {"message", "type", "code", "param"}}``.
A stream that fails after output has started ends with an error event and
``[DONE]`` and never sends a ``finish_reason``, so truncation is detectable.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from api.auth import AuthContext, require_scope
from coreai.errors import classify

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1", tags=["openai-compatible"])

_IGNORED = {
    "top_p", "seed", "user", "presence_penalty", "frequency_penalty",
    "logit_bias", "metadata", "store", "service_tier", "parallel_tool_calls",
}  # fmt: skip
_REFUSED = {
    "tools": "tool calling",
    "tool_choice": "tool calling",
    "functions": "function calling",
    "function_call": "function calling",
    "stop": "stop sequences",
    "logprobs": "logprobs",
    "top_logprobs": "logprobs",
    "modalities": "audio and other modalities",
    "audio": "audio",
    "prediction": "predicted outputs",
}


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: Optional[str] = None
    messages: list[dict[str, Any]] = Field(min_length=1)
    max_tokens: Optional[int] = Field(None, ge=1)
    max_completion_tokens: Optional[int] = Field(None, ge=1)
    temperature: Optional[float] = Field(None, ge=0, le=2)
    stream: bool = False
    stream_options: Optional[dict[str, Any]] = None
    n: Optional[int] = None
    response_format: Optional[dict[str, Any]] = None
    provider: Optional[str] = None


class CompatError(Exception):
    def __init__(self, status, message, type_, code=None, param=None, headers=None):
        self.status, self.message, self.type = status, message, type_
        self.code, self.param, self.headers = code, param, headers


def _envelope(message, type_, code=None, param=None) -> dict:
    return {"error": {"message": message, "type": type_, "param": param, "code": code}}


def _error_response(exc: CompatError) -> JSONResponse:
    return JSONResponse(
        _envelope(exc.message, exc.type, exc.code, exc.param),
        status_code=exc.status,
        headers=exc.headers,
    )


def _from_http(exc: HTTPException) -> CompatError:
    detail = exc.detail
    code = detail.get("error") if isinstance(detail, dict) else None
    message = (
        detail.get("message") or code or "request failed"
        if isinstance(detail, dict)
        else str(detail)
    )
    if exc.status_code == 429:
        type_ = "rate_limit_error"
    elif exc.status_code in (401, 403):
        type_ = "authentication_error"
    elif exc.status_code < 500:
        type_ = "invalid_request_error"
    else:
        type_ = "server_error"
    return CompatError(
        exc.status_code, str(message), type_, code, headers=dict(exc.headers or {})
    )


def _text_of(content: Any, index: int) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(str(part.get("text", "")))
            else:
                kind = part.get("type") if isinstance(part, dict) else "unknown"
                raise CompatError(
                    400,
                    f"Content part type '{kind}' is not supported yet (text only).",
                    "invalid_request_error",
                    "unsupported_parameter",
                    f"messages[{index}].content",
                )
        return "".join(parts)
    raise CompatError(
        400, "Message content must be a string or a list of text parts.",
        "invalid_request_error", "invalid_value", f"messages[{index}].content",
    )  # fmt: skip


def _convert(body: ChatRequest):
    """OpenAI request -> provider CompletionRequest."""
    from providers import CompletionRequest as PCR
    from providers import Message as PM

    for key, what in _REFUSED.items():
        if key in (body.model_extra or {}):
            raise CompatError(
                400, f"{what.capitalize()} is not supported yet.",
                "invalid_request_error", "unsupported_parameter", key,
            )  # fmt: skip
    if body.n not in (None, 1):
        raise CompatError(
            400, "Only n=1 is supported.", "invalid_request_error",
            "unsupported_parameter", "n",
        )  # fmt: skip
    if body.response_format and body.response_format.get("type") not in (None, "text"):
        raise CompatError(
            400, "response_format other than 'text' is not supported yet.",
            "invalid_request_error", "unsupported_parameter", "response_format",
        )  # fmt: skip

    system: list[str] = []
    messages = []
    for i, m in enumerate(body.messages):
        role = m.get("role")
        text = _text_of(m.get("content"), i)
        if role in ("system", "developer"):
            system.append(text)
        elif role in ("user", "assistant"):
            messages.append(PM(role=role, content=text))
        else:
            raise CompatError(
                400, f"Unsupported message role '{role}'.",
                "invalid_request_error", "invalid_value", f"messages[{i}].role",
            )  # fmt: skip
    if not messages:
        raise CompatError(
            400, "At least one user or assistant message is required.",
            "invalid_request_error", "invalid_value", "messages",
        )  # fmt: skip
    return PCR(
        messages=messages,
        model=body.model,
        max_tokens=body.max_completion_tokens or body.max_tokens or 1024,
        temperature=0.7 if body.temperature is None else body.temperature,
        system_prompt="\n\n".join(system) or None,
        stream=body.stream,
    )


def _chunk(cid, created, model, delta, finish=None, usage=None) -> str:
    data = {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [] if usage is not None else [
            {"index": 0, "delta": delta, "finish_reason": finish}
        ],
    }  # fmt: skip
    if usage is not None:
        data["usage"] = usage
    return f"data: {json.dumps(data, separators=(',', ':'))}\n\n"


@router.post("/chat/completions", summary="Chat completion (OpenAI-compatible)")
async def chat_completions(
    body: ChatRequest,
    ctx: AuthContext = Depends(require_scope("completions:write")),
):
    from api import routes
    from api.server import get_router

    try:
        preq = _convert(body)
        routes._budget_check(ctx, body.model)
        _router = get_router()
        cid = "chatcmpl-" + uuid.uuid4().hex[:24]
        created = int(time.time())

        if not body.stream:
            try:
                resp = await _router.route(preq, preferred_provider=body.provider)
            except Exception as exc:
                logger.error("Chat completion error for %s: %s", ctx.subject, exc)
                raise routes._http_error(exc)
            tokens = (resp.input_tokens or 0) + (resp.output_tokens or 0)
            routes._budget_record(ctx, resp.cost_usd, tokens, model=resp.model)
            routes._ledger_record(
                ctx, resp.provider, resp.model,
                resp.input_tokens, resp.output_tokens, resp.cost_usd,
            )  # fmt: skip
            return JSONResponse(
                {
                    "id": cid,
                    "object": "chat.completion",
                    "created": created,
                    "model": resp.model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": resp.content},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": resp.input_tokens or 0,
                        "completion_tokens": resp.output_tokens or 0,
                        "total_tokens": tokens,
                    },
                },
                headers={
                    "X-CaiPS-Provider": resp.provider,
                    "X-CaiPS-Cost-USD": f"{resp.cost_usd:.6f}",
                },
            )

        # Streaming. Open first so failures before any output become real
        # HTTP errors (with failover applied) instead of a 200 with an error.
        stream = _router.stream(preq, preferred_provider=body.provider)
        try:
            first = await stream.__anext__()
        except StopAsyncIteration:
            first = None
        except Exception as exc:
            logger.error("Chat stream error for %s: %s", ctx.subject, exc)
            raise routes._http_error(exc)
        routes._budget_record(ctx)
    except CompatError as exc:
        return _error_response(exc)
    except HTTPException as exc:
        return _error_response(_from_http(exc))

    # The router has already picked a provider and model; report the real one.
    usage = preq.stream_usage
    model_name = (
        (usage.opened_model if usage is not None and usage.opened_model else None)
        or body.model
        or "auto"
    )
    include_usage = bool((body.stream_options or {}).get("include_usage"))

    async def _generate():
        ok = False
        try:
            yield _chunk(cid, created, model_name, {"role": "assistant", "content": ""})
            if first is not None:
                yield _chunk(cid, created, model_name, {"content": first})
                async for text in stream:
                    yield _chunk(cid, created, model_name, {"content": text})
            ok = True
        except Exception as exc:
            logger.error("Chat stream error for %s: %s", ctx.subject, exc)
            kind = classify(exc).kind.value
            err = _envelope(
                f"The upstream provider failed mid-stream ({kind}).",
                "server_error", f"stream_{kind}",
            )  # fmt: skip
            yield f"data: {json.dumps(err, separators=(',', ':'))}\n\n"
        finally:
            await stream.aclose()
            routes._budget_record_stream(ctx, preq.stream_usage)
        if ok:
            yield _chunk(cid, created, model_name, {}, finish="stop")
            u = preq.stream_usage
            if include_usage and u is not None:
                yield _chunk(
                    cid, created, model_name, {},
                    usage={
                        "prompt_tokens": u.input_tokens,
                        "completion_tokens": u.output_tokens,
                        "total_tokens": u.input_tokens + u.output_tokens,
                    },
                )  # fmt: skip
        yield "data: [DONE]\n\n"

    return StreamingResponse(_generate(), media_type="text/event-stream")


@router.get("/models", summary="List models (OpenAI-compatible)")
async def list_models_openai(
    ctx: AuthContext = Depends(require_scope("completions:read")),
):
    """Models of the providers this gateway has active. The richer registry
    (prices, context windows, capabilities) is at ``/v1/models/details``."""
    from api.server import get_router
    from neural.model import get_registry

    try:
        active = set(get_router().providers)
        defaults = {n: p.default_model for n, p in get_router().providers.items()}
    except HTTPException as exc:
        return _error_response(_from_http(exc))
    seen: dict[str, str] = {}
    for p in get_registry().list():
        if p.provider in active:
            seen[p.model_id] = p.provider
    for name, model in defaults.items():
        if model:
            seen.setdefault(model, name)
    return {
        "object": "list",
        "data": [
            {"id": mid, "object": "model", "created": 0, "owned_by": owner}
            for mid, owner in sorted(seen.items())
        ],
    }
