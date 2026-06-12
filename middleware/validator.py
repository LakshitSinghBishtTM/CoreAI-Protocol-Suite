"""
CoreAI Protocol Suite - Validator Middleware
Request body validation beyond what Pydantic covers.
Checks message structure, content policy limits, and provider availability.
"""

from typing import Optional

from fastapi import Request, HTTPException
from loguru import logger


# Hard limits — requests exceeding these are rejected before hitting providers
MAX_MESSAGES = 50
MAX_MESSAGE_CHARS = 32_000    # per message
MAX_TOTAL_CHARS = 128_000     # across all messages in one request
MAX_MAX_TOKENS = 8_192


class ValidationError(HTTPException):
    def __init__(self, detail: str):
        super().__init__(status_code=422, detail=detail)


def validate_messages(messages: list[dict]) -> list[dict]:
    """
    Validate message list structure and content limits.
    Returns the validated messages or raises ValidationError.
    """
    if not messages:
        raise ValidationError("messages must be a non-empty list")

    if len(messages) > MAX_MESSAGES:
        raise ValidationError(
            f"Too many messages: {len(messages)} (max {MAX_MESSAGES})"
        )

    valid_roles = {"system", "user", "assistant"}
    total_chars = 0

    for i, msg in enumerate(messages):
        if not isinstance(msg, dict):
            raise ValidationError(f"messages[{i}] must be an object")

        role = msg.get("role")
        content = msg.get("content")

        if role not in valid_roles:
            raise ValidationError(
                f"messages[{i}].role must be one of {sorted(valid_roles)}, got '{role}'"
            )

        if not isinstance(content, str) or not content.strip():
            raise ValidationError(
                f"messages[{i}].content must be a non-empty string"
            )

        if len(content) > MAX_MESSAGE_CHARS:
            raise ValidationError(
                f"messages[{i}].content exceeds {MAX_MESSAGE_CHARS} characters "
                f"({len(content)} chars)"
            )

        total_chars += len(content)

    if total_chars > MAX_TOTAL_CHARS:
        raise ValidationError(
            f"Total message content exceeds {MAX_TOTAL_CHARS} characters "
            f"({total_chars} chars)"
        )

    return messages


def validate_max_tokens(max_tokens: Optional[int]) -> int:
    """Clamp and validate max_tokens."""
    if max_tokens is None:
        return 1024
    if not isinstance(max_tokens, int) or max_tokens < 1:
        raise ValidationError("max_tokens must be a positive integer")
    if max_tokens > MAX_MAX_TOKENS:
        raise ValidationError(
            f"max_tokens exceeds limit of {MAX_MAX_TOKENS} (got {max_tokens})"
        )
    return max_tokens


def validate_temperature(temperature: Optional[float]) -> float:
    """Validate temperature is in [0.0, 2.0]."""
    if temperature is None:
        return 0.7
    if not isinstance(temperature, (int, float)):
        raise ValidationError("temperature must be a number")
    if not (0.0 <= temperature <= 2.0):
        raise ValidationError(
            f"temperature must be between 0.0 and 2.0 (got {temperature})"
        )
    return float(temperature)


def validate_provider(
    provider: Optional[str],
    available_providers: set[str],
) -> Optional[str]:
    """
    Validate requested provider is loaded and available.
    Returns None (auto-route) if provider is None.
    """
    if provider is None:
        return None
    if provider not in available_providers:
        raise ValidationError(
            f"Provider '{provider}' not available. "
            f"Available: {sorted(available_providers)}"
        )
    return provider


def validate_completion_request(
    body: dict,
    available_providers: set[str],
) -> dict:
    """
    Full validation pass for /v1/completions request body.
    Returns cleaned/defaulted body dict.
    """
    body["messages"]    = validate_messages(body.get("messages", []))
    body["max_tokens"]  = validate_max_tokens(body.get("max_tokens"))
    body["temperature"] = validate_temperature(body.get("temperature"))
    body["provider"]    = validate_provider(body.get("provider"), available_providers)
    return body
