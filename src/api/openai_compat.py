"""OpenAI-compatible endpoints: ``/v1/chat/completions`` and ``/v1/models``.

Point an OpenAI SDK at ``<host>/v1`` and pass a CaiPS token as the API key.

Supported: text messages (system, developer, user, assistant), ``model``,
``max_tokens`` / ``max_completion_tokens``, ``temperature``, ``stream`` and
``stream_options.include_usage``. Accepted and ignored: ``top_p``, ``seed``,
``user``, ``presence_penalty``, ``frequency_penalty``, ``logit_bias``.
Tool calling (``tools``, ``tool_choice``, assistant ``tool_calls`` and
``tool`` messages) and ``response_format`` (``json_object``, ``json_schema``)
work without streaming; the gateway returns tool calls and never runs them.
Image inputs (``image_url`` parts on user messages, http(s) or base64 data URLs)
are accepted for models that take images. Anything else that would change the
answer (``stop``, ``n`` above 1, audio parts, tools with ``stream=true``) is refused with a 400 rather than silently
dropped. Extension field: ``provider`` pins a provider.

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
    tools: Optional[list[dict[str, Any]]] = None
    tool_choice: Optional[Any] = None
    provider: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None
    policy: Optional[str] = None
    # CaiPS extension: let the gateway run the tools this caller is granted
    # server-side and continue the conversation. Off by default.
    run_tools: bool = False


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


def _bad(message, param, code="invalid_value") -> CompatError:
    return CompatError(400, message, "invalid_request_error", code, param)


def _check_tools(body: ChatRequest) -> None:
    for i, t in enumerate(body.tools or []):
        fn = t.get("function") if isinstance(t, dict) else None
        if (
            not isinstance(fn, dict)
            or t.get("type") != "function"
            or not fn.get("name")
        ):
            raise _bad(
                "Each tool must be {'type': 'function', 'function': {'name': ...}}.",
                f"tools[{i}]",
            )
    choice = body.tool_choice
    if choice is None:
        return
    if not body.tools:
        raise _bad("tool_choice needs tools.", "tool_choice")
    if isinstance(choice, str):
        if choice not in ("auto", "none", "required"):
            raise _bad(
                "tool_choice must be auto, none, required or a function.", "tool_choice"
            )
    elif isinstance(choice, dict):
        name = (choice.get("function") or {}).get("name")
        if choice.get("type") != "function" or name not in {
            t["function"]["name"] for t in body.tools
        }:
            raise _bad("tool_choice must name one of the given tools.", "tool_choice")
    else:
        raise _bad("tool_choice must be a string or an object.", "tool_choice")


def _check_response_format(rf: Optional[dict]) -> None:
    if not rf or rf.get("type") in (None, "text"):
        return
    kind = rf.get("type")
    if kind == "json_object":
        return
    if kind == "json_schema":
        js = rf.get("json_schema")
        if not isinstance(js, dict) or not isinstance(js.get("schema"), dict):
            raise _bad(
                "response_format.json_schema.schema must be an object.",
                "response_format",
            )
        return
    raise CompatError(
        400, f"response_format type '{kind}' is not supported.",
        "invalid_request_error", "unsupported_parameter", "response_format",
    )  # fmt: skip


MAX_IMAGE_BYTES = 20_000_000  # base64 characters of inline images per request


def _image(obj: Any, index: int) -> dict:
    from providers.base import IMAGE_TYPES, parse_data_url

    param = f"messages[{index}].content"
    url = obj.get("url") if isinstance(obj, dict) else obj
    if not isinstance(url, str) or not url:
        raise _bad("image_url.url must be a string.", param)
    if url.startswith("data:"):
        img = parse_data_url(url)
        if img is None or img["media_type"] not in IMAGE_TYPES:
            raise _bad(
                f"Images must be base64 data URLs of type {', '.join(IMAGE_TYPES)}.",
                param,
            )
        return img
    if url.startswith(("http://", "https://")):
        return {"url": url}
    raise _bad("image_url.url must be an http(s) or data URL.", param)


def _content(content: Any, index: int, allow_images: bool) -> tuple[str, list[dict]]:
    """Text and images of a message. Images are accepted on user messages."""
    if not (allow_images and isinstance(content, list)):
        return _text_of(content, index), []
    text, images = [], []
    for part in content:
        if isinstance(part, dict) and part.get("type") == "image_url":
            images.append(_image(part.get("image_url"), index))
        else:
            text.append(_text_of([part], index))
    return "".join(text), images


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
    _check_response_format(body.response_format)
    _check_tools(body)

    system: list[str] = []
    messages = []
    for i, m in enumerate(body.messages):
        role = m.get("role")
        text, images = _content(
            m.get("content"), i, allow_images=m.get("role") == "user"
        )
        if role in ("system", "developer"):
            system.append(text)
        elif role == "tool":
            if not m.get("tool_call_id"):
                raise _bad(
                    "A tool message needs tool_call_id.", f"messages[{i}].tool_call_id"
                )
            messages.append(
                PM(role="tool", content=text, tool_call_id=str(m["tool_call_id"]))
            )
        elif role == "assistant" and m.get("tool_calls"):
            messages.append(
                PM(
                    role=role,
                    content=text,
                    tool_calls=_request_calls(m["tool_calls"], i),
                )
            )
        elif role in ("user", "assistant"):
            messages.append(PM(role=role, content=text, images=images or None))
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
    if (
        sum(len(i.get("data", "")) for m in messages for i in m.images or [])
        > MAX_IMAGE_BYTES
    ):
        raise _bad(
            "Inline images are too large for one request.",
            "messages",
            "request_too_large",
        )
    return PCR(
        messages=messages,
        model=body.model,
        max_tokens=body.max_completion_tokens or body.max_tokens or 1024,
        temperature=0.7 if body.temperature is None else body.temperature,
        system_prompt="\n\n".join(system) or None,
        stream=body.stream,
        metadata=_policy_metadata(body),
        tools=body.tools or None,
        tool_choice=body.tool_choice,
        response_format=(
            body.response_format
            if (body.response_format or {}).get("type") != "text"
            else None
        ),
    )


def _request_calls(raw: Any, index: int) -> list[dict]:
    """An assistant message's ``tool_calls`` in normalised form."""
    param = f"messages[{index}].tool_calls"
    if not isinstance(raw, list):
        raise _bad("tool_calls must be a list.", param)
    calls = []
    for c in raw:
        fn = c.get("function") if isinstance(c, dict) else None
        if not isinstance(fn, dict) or not c.get("id") or not fn.get("name"):
            raise _bad("Each tool call needs an id and a function name.", param)
        args = fn.get("arguments")
        calls.append(
            {
                "id": str(c["id"]),
                "name": fn["name"],
                "arguments": args if isinstance(args, str) else json.dumps(args or {}),
            }
        )
    return calls


def _policy_metadata(body) -> dict:
    """Named routing policy from ``metadata.policy`` (or a top-level
    ``policy`` field), so OpenAI-compatible clients can pick one."""
    raw = body.metadata if isinstance(getattr(body, "metadata", None), dict) else {}
    name = raw.get("policy") or getattr(body, "policy", None)
    meta: dict = {"policy": name} if isinstance(name, str) and name else {}
    if raw.get("private") is True:
        meta["private"] = True  # local providers only
    return meta


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


def _buffered_stream(cid, created, resp, finish, body, tokens) -> StreamingResponse:
    msg = _assistant_message(resp)

    def _gen():
        yield _chunk(cid, created, resp.model, {"role": "assistant", "content": ""})
        if msg.get("tool_calls"):
            for i, c in enumerate(msg["tool_calls"]):
                yield _chunk(cid, created, resp.model, {"tool_calls": [{
                    "index": i, "id": c["id"], "type": "function",
                    "function": {"name": c["function"]["name"], "arguments": ""},
                }]})  # fmt: skip
                yield _chunk(cid, created, resp.model, {"tool_calls": [{
                    "index": i, "function": {"arguments": c["function"]["arguments"]},
                }]})  # fmt: skip
        if msg.get("content"):
            yield _chunk(cid, created, resp.model, {"content": msg["content"]})
        yield _chunk(cid, created, resp.model, {}, finish=finish)
        if (body.stream_options or {}).get("include_usage"):
            yield _chunk(cid, created, resp.model, {}, usage={
                "prompt_tokens": resp.input_tokens or 0,
                "completion_tokens": resp.output_tokens or 0,
                "total_tokens": tokens,
            })  # fmt: skip
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        _gen(), media_type="text/event-stream",
        headers={"X-CaiPS-Provider": resp.provider},
    )  # fmt: skip


def _assistant_message(resp) -> dict:
    calls = (resp.metadata or {}).get("tool_calls")
    if not calls:
        return {"role": "assistant", "content": resp.content}
    return {
        "role": "assistant",
        "content": resp.content or None,
        "tool_calls": [
            {
                "id": c["id"],
                "type": "function",
                "function": {"name": c["name"], "arguments": c["arguments"]},
            }
            for c in calls
        ],
    }


def _schema_of(rf: Optional[dict]) -> Optional[dict]:
    if rf and rf.get("type") == "json_schema":
        return rf["json_schema"]["schema"]
    return None


def _schema_error(content: str, schema: dict) -> Optional[str]:
    """Return a description of why ``content`` violates ``schema``, else None."""
    import jsonschema

    try:
        data = json.loads(content)
    except (TypeError, ValueError) as exc:
        return f"output is not valid JSON ({exc})"
    try:
        jsonschema.validate(data, schema)
    except jsonschema.ValidationError as exc:
        return exc.message
    except jsonschema.SchemaError as exc:
        return f"invalid schema: {exc.message}"
    return None


async def _route_with_tools(_router, preq, body, ctx):
    """Run the request with gateway-side tool execution. The tools offered
    come from server config for this subject, never from the request."""
    from api.server import app_state
    from tools.executor import run_tool_loop

    rt = app_state.get("tools")
    allowed = rt.allowed_for(ctx.subject) if rt is not None and rt.enabled else []
    if not allowed:
        raise CompatError(
            400, "Tool execution is not enabled for this key.",
            "invalid_request_error", "tools_not_enabled", "run_tools",
        )  # fmt: skip

    async def route(r):
        return await _router.route(r, preferred_provider=body.provider)

    out = await run_tool_loop(
        route, rt.registry, preq, allowed=allowed, caller=ctx.subject,
        approved=rt.approvals.consume, max_permission=rt.max_permission,
    )  # fmt: skip
    if out.stop_reason == "needs_approval":
        step = out.pending_approval
        a = rt.approvals.request(ctx.subject, step.tool, step.arguments)
        raise CompatError(
            409, f"Tool '{step.tool}' needs approval (id {a.id}). Approve it, then repeat the request.",
            "invalid_request_error", "approval_required", "run_tools",
        )  # fmt: skip
    resp = out.response
    resp.cost_usd = out.total_cost_usd
    resp.input_tokens, resp.output_tokens = out.input_tokens, out.output_tokens
    if out.stop_reason in ("max_steps", "cost_limit"):
        resp.metadata = {**(resp.metadata or {}), "tool_loop_stopped": out.stop_reason}
    return resp, out.steps


async def _route_validated(_router, preq, body):
    from providers import Message as PM

    """Route once; if a json_schema was requested, validate the output and
    retry once with the validation error before giving up."""
    preq.stream = False  # buffered: the reply is validated before it is sent
    resp = await _router.route(preq, preferred_provider=body.provider)
    schema = _schema_of(body.response_format)
    if schema is None or (resp.metadata or {}).get("tool_calls"):
        return resp
    err = _schema_error(resp.content, schema)
    if err is None:
        return resp
    retry = _convert(body)
    retry.stream = False
    retry.messages.append(PM(role="assistant", content=resp.content))
    retry.messages.append(
        PM(
            role="user",
            content=f"Your last reply did not match the JSON schema: {err}. "
            "Reply again with only JSON that matches the schema.",
        )
    )
    resp2 = await _router.route(retry, preferred_provider=body.provider)
    err2 = _schema_error(resp2.content, schema)
    if err2 is not None:
        raise CompatError(
            502, f"Model output did not match response_format schema: {err2}",
            "server_error", "schema_validation_failed", "response_format",
        )  # fmt: skip
    resp2.metadata = {
        **(resp.metadata or {}),
        **(resp2.metadata or {}),
        "schema_retried": True,
    }
    resp2.cost_usd += resp.cost_usd
    resp2.input_tokens = (resp2.input_tokens or 0) + (resp.input_tokens or 0)
    resp2.output_tokens = (resp2.output_tokens or 0) + (resp.output_tokens or 0)
    return resp2


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
        routes._bind_model_gate(_router)
        cid = "chatcmpl-" + uuid.uuid4().hex[:24]
        created = int(time.time())

        # Tool calls and schema-validated output need the whole reply before
        # anything can be emitted, so those streams are buffered, then sent as
        # SSE chunks (complete tool calls, validated JSON).
        buffered = (
            bool(body.tools)
            or body.run_tools
            or _schema_of(body.response_format) is not None
        )
        tool_steps = None
        if not body.stream or buffered:
            try:
                if body.run_tools:
                    resp, tool_steps = await _route_with_tools(_router, preq, body, ctx)
                else:
                    resp = await _route_validated(_router, preq, body)
            except CompatError:
                raise
            except Exception as exc:
                logger.error("Chat completion error for %s: %s", ctx.subject, exc)
                raise routes._http_error(exc)
            tokens = (resp.input_tokens or 0) + (resp.output_tokens or 0)
            routes._budget_record(ctx, resp.cost_usd, tokens, model=resp.model)
            routes._ledger_record(
                ctx, resp.provider, resp.model,
                resp.input_tokens, resp.output_tokens, resp.cost_usd,
            )  # fmt: skip
            finish = (resp.metadata or {}).get("finish_reason") or "stop"
            if body.stream:
                return _buffered_stream(cid, created, resp, finish, body, tokens)
            extra = {}
            if tool_steps is not None:
                extra["caips_tool_steps"] = [
                    {"tool": st.tool, "ok": st.ok, "error": st.error_code,
                     "duration_ms": round(st.duration_ms, 1)}
                    for st in tool_steps
                ]  # fmt: skip
            return JSONResponse(
                {
                    **extra,
                    "id": cid,
                    "object": "chat.completion",
                    "created": created,
                    "model": resp.model,
                    "choices": [
                        {
                            "index": 0,
                            "message": _assistant_message(resp),
                            "finish_reason": (resp.metadata or {}).get("finish_reason")
                            or "stop",
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
            u0 = preq.stream_usage
            yield _chunk(
                cid,
                created,
                model_name,
                {},
                finish=(u0.finish_reason if u0 is not None else None) or "stop",
            )
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


class EmbeddingsRequest(BaseModel):
    model: Optional[str] = None
    input: Any
    encoding_format: Optional[str] = None
    provider: Optional[str] = None


@router.post("/embeddings", summary="Create embeddings (OpenAI-compatible)")
async def create_embeddings(
    body: EmbeddingsRequest,
    ctx: AuthContext = Depends(require_scope("completions:write")),
):
    from api import routes
    from api.server import get_router

    raw = body.input
    texts = [raw] if isinstance(raw, str) else raw
    if (
        not isinstance(texts, list)
        or not texts
        or not all(isinstance(t, str) and t for t in texts)
    ):
        return _error_response(
            CompatError(
                400, "input must be a non-empty string or list of non-empty strings.",
                "invalid_request_error", "invalid_value", "input",
            )  # fmt: skip
        )
    if body.encoding_format not in (None, "float"):
        return _error_response(
            CompatError(
                400, "Only encoding_format 'float' is supported.",
                "invalid_request_error", "unsupported_parameter", "encoding_format",
            )  # fmt: skip
        )
    try:
        r = get_router()
        routes._budget_check(ctx, body.model)
        vectors, tokens, used, provider = await r.embed(
            texts, body.model, body.provider
        )
    except HTTPException as exc:
        return _error_response(_from_http(exc))
    except ValueError as exc:
        return _error_response(
            CompatError(400, str(exc), "invalid_request_error", "invalid_value")
        )
    except Exception as exc:
        logger.error("Embeddings error for %s: %s", ctx.subject, exc)
        kind = classify(exc).kind.value
        return _error_response(
            CompatError(
                502, f"The upstream provider failed ({kind}).", "server_error", kind
            )
        )
    cost = r.providers[provider]._bill(tokens, 0, used)
    routes._budget_record(ctx, cost, tokens, model=used)
    routes._ledger_record(ctx, provider, used, tokens, 0, cost)
    return {
        "object": "list",
        "model": used,
        "data": [
            {"object": "embedding", "index": i, "embedding": v}
            for i, v in enumerate(vectors)
        ],
        "usage": {"prompt_tokens": tokens, "total_tokens": tokens},
    }


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
    data = [
        {"id": mid, "object": "model", "created": 0, "owned_by": owner}
        for mid, owner in sorted(seen.items())
    ]
    # Virtual models: names that map to a routing policy.
    r = get_router()
    for alias in sorted(r.aliases):
        if r.alias_policy(alias):
            data.append(
                {"id": alias, "object": "model", "created": 0, "owned_by": "caips"}
            )
    return {"object": "list", "data": data}
