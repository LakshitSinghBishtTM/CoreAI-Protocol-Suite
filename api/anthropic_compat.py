"""Anthropic-compatible endpoint: ``POST /v1/messages``.

Point an Anthropic SDK at the gateway (``base_url=<host>``) for clients that
expect that API. Auth is the usual CaiPS token (``Authorization: Bearer``) or
an API key (``x-api-key``).

Supported: text, image (base64 or URL) and ``tool_use`` / ``tool_result``
blocks, ``system`` (string or text blocks), ``max_tokens``, ``temperature``,
``tools``, ``tool_choice``, and ``stream`` (text only). Accepted and ignored:
``top_p``, ``top_k``, ``metadata``. Refused with a 400: ``stop_sequences``,
other block types (documents, thinking), tools with ``stream=true``.
Extension field: ``provider`` pins a provider. Errors use Anthropic's
envelope ``{"type": "error", "error": {"type", "message"}}``.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from api.auth import AuthContext, require_scope
from api.openai_compat import MAX_IMAGE_BYTES, CompatError, _from_http
from coreai.errors import classify

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1", tags=["anthropic-compatible"])

_ERROR_TYPES = {
    "invalid_request_error": "invalid_request_error",
    "authentication_error": "authentication_error",
    "rate_limit_error": "rate_limit_error",
    "server_error": "api_error",
}
_STOP_REASONS = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "content_filter": "refusal",
}
_IGNORED = {"top_p", "top_k", "metadata", "service_tier", "container"}


class MessagesRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: Optional[str] = None
    max_tokens: int = Field(ge=1)
    messages: list[dict[str, Any]] = Field(min_length=1)
    system: Optional[Any] = None
    temperature: Optional[float] = Field(None, ge=0, le=1)
    stream: bool = False
    tools: Optional[list[dict[str, Any]]] = None
    tool_choice: Optional[dict[str, Any]] = None
    provider: Optional[str] = None


def _bad(message: str, param: Optional[str] = None) -> CompatError:
    return CompatError(400, message, "invalid_request_error", "invalid_value", param)


def _error(exc: CompatError) -> JSONResponse:
    kind = _ERROR_TYPES.get(exc.type, "api_error")
    return JSONResponse(
        {"type": "error", "error": {"type": kind, "message": exc.message}},
        status_code=exc.status,
        headers=exc.headers,
    )


def _text(content: Any, where: str) -> str:
    """Plain text of a ``system`` or ``tool_result`` content value."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list) and all(
        isinstance(b, dict) and b.get("type") == "text" for b in content
    ):
        return "".join(str(b.get("text", "")) for b in content)
    raise _bad(f"{where} must be a string or a list of text blocks.", where)


def _image(block: dict, where: str) -> dict:
    from providers.base import IMAGE_TYPES

    src = block.get("source")
    if not isinstance(src, dict):
        raise _bad("An image block needs a source.", where)
    if src.get("type") == "base64":
        media = str(src.get("media_type", "")).lower()
        if media not in IMAGE_TYPES or not src.get("data"):
            raise _bad(
                f"Images must be base64 of type {', '.join(IMAGE_TYPES)}.", where
            )
        return {"media_type": media, "data": src["data"]}
    if src.get("type") == "url" and str(src.get("url", "")).startswith(
        ("http://", "https://")
    ):
        return {"url": src["url"]}
    raise _bad("Image source must be base64 or an http(s) url.", where)


def _convert(body: MessagesRequest):
    from providers import CompletionRequest as PCR
    from providers import Message as PM

    if "stop_sequences" in (body.model_extra or {}):
        raise CompatError(
            400,
            "Stop sequences are not supported yet.",
            "invalid_request_error",
            "unsupported_parameter",
            "stop_sequences",
        )
    if body.tools and body.stream:
        raise CompatError(
            400,
            "Tool use with stream=true is not supported yet.",
            "invalid_request_error",
            "unsupported_parameter",
            "tools",
        )

    messages = []
    for i, m in enumerate(body.messages):
        role, content = m.get("role"), m.get("content")
        where = f"messages[{i}].content"
        if role not in ("user", "assistant"):
            raise _bad(f"Unsupported message role '{role}'.", f"messages[{i}].role")
        if isinstance(content, str):
            messages.append(PM(role=role, content=content))
            continue
        if not isinstance(content, list):
            raise _bad("Message content must be a string or a list of blocks.", where)
        texts, images, calls, results = [], [], [], []
        for b in content:
            kind = b.get("type") if isinstance(b, dict) else None
            if kind == "text":
                texts.append(str(b.get("text", "")))
            elif kind == "image" and role == "user":
                images.append(_image(b, where))
            elif kind == "tool_use" and role == "assistant":
                if not b.get("id") or not b.get("name"):
                    raise _bad("A tool_use block needs id and name.", where)
                calls.append(
                    {
                        "id": b["id"],
                        "name": b["name"],
                        "arguments": json.dumps(b.get("input") or {}),
                    }
                )
            elif kind == "tool_result" and role == "user":
                if not b.get("tool_use_id"):
                    raise _bad("A tool_result block needs tool_use_id.", where)
                text = _text(b.get("content"), where)
                results.append(
                    PM(role="tool", content=text, tool_call_id=b["tool_use_id"])
                )
            else:
                raise CompatError(
                    400, f"Content block type '{kind}' is not supported for {role} messages.",
                    "invalid_request_error", "unsupported_parameter", where,
                )  # fmt: skip
        messages.extend(results)  # results come first in a user turn
        if texts or images or calls:
            messages.append(
                PM(
                    role=role,
                    content="".join(texts),
                    images=images or None,
                    tool_calls=calls or None,
                )
            )
    if (
        sum(len(i.get("data", "")) for m in messages for i in m.images or [])
        > MAX_IMAGE_BYTES
    ):
        raise _bad("Inline images are too large for one request.", "messages")

    system = (
        body.system if isinstance(body.system, str) else _text(body.system, "system")
    )
    return PCR(
        messages=messages,
        model=body.model,
        max_tokens=body.max_tokens,
        temperature=0.7 if body.temperature is None else body.temperature,
        system_prompt=system or None,
        stream=body.stream,
        tools=_tools(body.tools),
        tool_choice=_tool_choice(body.tool_choice, body.tools),
    )


def _tools(raw: Optional[list[dict]]) -> Optional[list[dict]]:
    out = []
    for i, t in enumerate(raw or []):
        if (
            not isinstance(t, dict)
            or not t.get("name")
            or t.get("type", "custom") != "custom"
        ):
            raise _bad(
                "Each tool needs a name and an input_schema (server tools are not supported).",
                f"tools[{i}]",
            )
        out.append(
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema")
                    or {"type": "object", "properties": {}},
                },
            }
        )
    return out or None


def _tool_choice(choice: Optional[dict], tools: Optional[list]):
    if choice is None:
        return None
    if not tools:
        raise _bad("tool_choice needs tools.", "tool_choice")
    kind = choice.get("type")
    if kind == "tool":
        if choice.get("name") not in {t.get("name") for t in tools}:
            raise _bad("tool_choice must name one of the given tools.", "tool_choice")
        return {"type": "function", "function": {"name": choice["name"]}}
    mapped = {"auto": "auto", "any": "required", "none": "none"}.get(kind)
    if mapped is None:
        raise _bad("tool_choice type must be auto, any, none or tool.", "tool_choice")
    return mapped


def _blocks(resp) -> list[dict]:
    blocks = []
    if resp.content:
        blocks.append({"type": "text", "text": resp.content})
    for c in (resp.metadata or {}).get("tool_calls") or []:
        try:
            args = json.loads(c["arguments"] or "{}")
        except ValueError:
            args = {}
        blocks.append(
            {"type": "tool_use", "id": c["id"], "name": c["name"], "input": args}
        )
    return blocks or [{"type": "text", "text": ""}]


def _event(name: str, data: dict) -> str:
    return f"event: {name}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


@router.post("/messages", summary="Messages (Anthropic-compatible)")
async def create_message(
    body: MessagesRequest,
    ctx: AuthContext = Depends(require_scope("completions:write")),
):
    from api import routes
    from api.server import get_router

    try:
        preq = _convert(body)
        routes._budget_check(ctx, body.model)
        _router = get_router()
        routes._bind_model_gate(_router)
        mid = "msg_" + uuid.uuid4().hex[:24]

        if not body.stream:
            try:
                resp = await _router.route(preq, preferred_provider=body.provider)
            except Exception as exc:
                logger.error("Messages error for %s: %s", ctx.subject, exc)
                raise routes._http_error(exc)
            tokens = (resp.input_tokens or 0) + (resp.output_tokens or 0)
            routes._budget_record(ctx, resp.cost_usd, tokens, model=resp.model)
            routes._ledger_record(
                ctx, resp.provider, resp.model, resp.input_tokens, resp.output_tokens, resp.cost_usd
            )  # fmt: skip
            reason = (resp.metadata or {}).get("finish_reason") or "stop"
            return JSONResponse(
                {
                    "id": mid,
                    "type": "message",
                    "role": "assistant",
                    "model": resp.model,
                    "content": _blocks(resp),
                    "stop_reason": _STOP_REASONS.get(reason, "end_turn"),
                    "stop_sequence": None,
                    "usage": {
                        "input_tokens": resp.input_tokens or 0,
                        "output_tokens": resp.output_tokens or 0,
                    },
                },
                headers={
                    "X-CaiPS-Provider": resp.provider,
                    "X-CaiPS-Cost-USD": f"{resp.cost_usd:.6f}",
                },
            )

        stream = _router.stream(preq, preferred_provider=body.provider)
        try:
            first = await stream.__anext__()
        except StopAsyncIteration:
            first = None
        except Exception as exc:
            logger.error("Messages stream error for %s: %s", ctx.subject, exc)
            raise routes._http_error(exc)
        routes._budget_record(ctx)
    except CompatError as exc:
        return _error(exc)
    except HTTPException as exc:
        return _error(_from_http(exc))

    usage = preq.stream_usage
    model_name = (
        (usage.opened_model if usage is not None and usage.opened_model else None)
        or body.model
        or "auto"
    )

    async def _generate():
        ok = False
        try:
            yield _event(
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": mid, "type": "message", "role": "assistant", "model": model_name,
                        "content": [], "stop_reason": None, "stop_sequence": None,
                        "usage": {"input_tokens": 0, "output_tokens": 0},
                    },
                },
            )  # fmt: skip
            yield _event(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
            )

            def delta(text):
                return _event(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": text},
                    },
                )

            if first is not None:
                yield delta(first)
                async for text in stream:
                    yield delta(text)
            ok = True
        except Exception as exc:
            logger.error("Messages stream error for %s: %s", ctx.subject, exc)
            kind = classify(exc).kind.value
            yield _event(
                "error",
                {
                    "type": "error",
                    "error": {
                        "type": "api_error",
                        "message": f"The upstream provider failed mid-stream ({kind}).",
                    },
                },
            )
        finally:
            await stream.aclose()
            routes._budget_record_stream(ctx, preq.stream_usage)
        if ok:
            u = preq.stream_usage
            reason = (u.finish_reason if u is not None else None) or "stop"
            yield _event(
                "content_block_stop", {"type": "content_block_stop", "index": 0}
            )
            yield _event(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {
                        "stop_reason": _STOP_REASONS.get(reason, "end_turn"),
                        "stop_sequence": None,
                    },
                    "usage": {"output_tokens": u.output_tokens if u is not None else 0},
                },
            )
            yield _event("message_stop", {"type": "message_stop"})

    return StreamingResponse(_generate(), media_type="text/event-stream")
