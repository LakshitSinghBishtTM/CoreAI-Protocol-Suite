"""One logging pipeline: loguru, as JSON lines, with a request ID on every line.

* ``configure_logging()`` replaces loguru's default sink with a JSON (or text)
  sink to stderr and, if ``COREAI_LOG_FILE`` is set, to a rotating file with a
  size cap and a bounded number of old files.
* Standard-library ``logging`` (uvicorn, FastAPI, the few modules that still use
  it) is routed into the same pipeline.
* The request ID comes from a context variable set by ``RequestContextMiddleware``
  (``coreai/request_context.py``); every line logged while serving a request
  carries it.
* Obvious secrets (bearer tokens, ``sk-`` style keys) are masked in messages.

Environment: ``COREAI_LOG_FORMAT`` (json|text, default json),
``COREAI_LOG_LEVEL`` (default INFO), ``COREAI_LOG_FILE``,
``COREAI_LOG_ROTATION`` (default ``50 MB``), ``COREAI_LOG_RETENTION`` (files kept,
default 5).
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from typing import Any

from loguru import logger

from coreai import tracing
from coreai.request_context import current_request_id

_SECRET_PATTERNS = (
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 [redacted]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), "sk-[redacted]"),
)
_RESERVED = {"ts", "level", "logger", "message", "request_id", "exception"}


def redact(text: str) -> str:
    """Mask secrets, then PII (on by default; ``COREAI_LOG_REDACT_PII=0`` turns PII off)."""
    for pattern, repl in _SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    if os.getenv("COREAI_LOG_REDACT_PII", "1").lower() not in ("0", "false", "no"):
        from guardrails.pii import redact as pii_redact

        text = pii_redact(text)
    return text


def _patcher(record: dict) -> None:
    record["extra"].setdefault("request_id", current_request_id())
    ids = tracing.current_ids()
    if ids is not None:
        record["extra"].setdefault("trace_id", ids[0])
        record["extra"].setdefault("span_id", ids[1])


def format_json(record: dict) -> str:
    """One JSON object per line. Extra fields bound with ``logger.bind`` are
    included at the top level (names that would clash are prefixed ``x_``)."""
    out: dict[str, Any] = {
        "ts": record["time"].astimezone().isoformat(timespec="milliseconds"),
        "level": record["level"].name,
        "logger": f"{record['name']}:{record['function']}",
        "message": redact(record["message"]),
        "request_id": record["extra"].get("request_id"),
    }
    for key, value in record["extra"].items():
        if key == "request_id" or key.startswith("_"):
            continue
        out[f"x_{key}" if key in _RESERVED else key] = value
    if record["exception"] is not None:
        exc = record["exception"]
        out["exception"] = redact(f"{exc.type.__name__}: {exc.value}")
    return json.dumps(out, default=str, separators=(",", ":"))


def _file_format(record: dict) -> str:
    # loguru treats a format callable's result as a template, so the JSON line
    # is stashed in ``extra`` and referenced by a fixed template.
    record["extra"]["_json"] = format_json(record)
    return "{extra[_json]}\n"


def _json_sink(stream):
    def write(message) -> None:
        stream.write(format_json(message.record) + "\n")
        stream.flush()

    return write


def _text_format(record: dict) -> str:
    rid = record["extra"].get("request_id") or "-"
    msg = redact(record["message"]).replace("{", "{{").replace("}", "}}")
    return (
        "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | "
        f"{rid} | {{name}}:{{function}} - {msg}\n"
    )


class InterceptHandler(logging.Handler):
    """Send standard-library log records to loguru."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame, depth = logging.currentframe(), 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )


def configure_logging(
    fmt: str | None = None,
    level: str | None = None,
    file: str | None = None,
    rotation: str | None = None,
    retention: int | None = None,
    stream=None,
) -> None:
    """Idempotent: safe to call again (for example in tests)."""
    fmt = (fmt or os.environ.get("COREAI_LOG_FORMAT", "json")).lower()
    if fmt not in ("json", "text"):
        raise ValueError("log format must be 'json' or 'text'")
    level = (level or os.environ.get("COREAI_LOG_LEVEL", "INFO")).upper()
    file = file or os.environ.get("COREAI_LOG_FILE") or None
    rotation = rotation or os.environ.get("COREAI_LOG_ROTATION", "50 MB")
    retention = retention or int(os.environ.get("COREAI_LOG_RETENTION", "5"))
    stream = stream or sys.stderr

    logger.remove()
    logger.configure(patcher=_patcher)
    if fmt == "json":
        logger.add(_json_sink(stream), level=level)
    else:
        logger.add(stream, level=level, format=_text_format)
    if file:
        # Rotation and retention are what keep a log file from growing forever.
        logger.add(
            file,
            level=level,
            rotation=rotation,
            retention=retention,
            format=_file_format if fmt == "json" else _text_format,
        )

    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "fastapi"):
        lg = logging.getLogger(name)
        lg.handlers = [InterceptHandler()]
        lg.propagate = False
