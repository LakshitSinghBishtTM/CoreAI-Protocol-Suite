"""
CoreAI Protocol Suite - Token Utilities
Token counting and estimation across providers.
Each provider has different tokenization — these are fast approximations
for routing/budgeting decisions, not billing-accurate counts.
"""

import re
from typing import Optional

# Approximate chars-per-token ratios by provider/model family
# Based on empirical sampling — close enough for cost estimation
CHARS_PER_TOKEN = {
    "openai": 4.0,  # GPT tokenizer (tiktoken)
    "anthropic": 4.0,  # Claude tokenizer (similar to GPT)
    "gemini": 3.8,  # Gemini tokenizes slightly more aggressively
    "deepseek": 4.0,  # OpenAI-compatible tokenizer
    "grok": 4.0,  # xAI tokenizer (similar to GPT)
    "default": 4.0,
}

# Token overhead per message role (system/user/assistant framing)
ROLE_OVERHEAD = {
    "system": 4,
    "user": 4,
    "assistant": 4,
}


def estimate_tokens(text: str, provider: str = "default") -> int:
    """
    Fast token estimate for a single string.
    Uses chars-per-token ratio for the given provider.
    """
    if not text:
        return 0
    ratio = CHARS_PER_TOKEN.get(provider, CHARS_PER_TOKEN["default"])
    return max(1, int(len(text) / ratio))


def estimate_messages_tokens(
    messages: list[dict],
    provider: str = "default",
) -> int:
    """
    Estimate total tokens for a messages list (role + content).
    Includes per-message role overhead.
    """
    total = 0
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        total += estimate_tokens(content, provider)
        total += ROLE_OVERHEAD.get(role, 4)
    return total


def estimate_request_tokens(
    messages: list[dict],
    system_prompt: Optional[str],
    provider: str = "default",
) -> int:
    """
    Full request token estimate including system prompt.
    Use for rate-limit pre-checks in the router.
    """
    total = estimate_messages_tokens(messages, provider)
    if system_prompt:
        total += estimate_tokens(system_prompt, provider)
        total += ROLE_OVERHEAD["system"]
    return total


def tokens_to_chars(tokens: int, provider: str = "default") -> int:
    """Inverse of estimate_tokens — approximate char count for a token budget."""
    ratio = CHARS_PER_TOKEN.get(provider, CHARS_PER_TOKEN["default"])
    return int(tokens * ratio)


def truncate_to_token_budget(
    text: str,
    max_tokens: int,
    provider: str = "default",
) -> str:
    """
    Truncate text to fit within a token budget.
    Truncates at word boundaries where possible.
    """
    if estimate_tokens(text, provider) <= max_tokens:
        return text

    char_budget = tokens_to_chars(max_tokens, provider)
    truncated = text[:char_budget]

    # Try to cut at last whitespace to avoid mid-word truncation
    last_space = truncated.rfind(" ")
    if last_space > char_budget * 0.8:
        truncated = truncated[:last_space]

    return truncated


def count_words(text: str) -> int:
    """Word count — rough proxy for output length estimation."""
    return len(re.findall(r"\b\w+\b", text))


def format_token_count(tokens: int) -> str:
    """Human-readable token count string."""
    if tokens >= 1_000_000:
        return f"{tokens / 1_000_000:.1f}M"
    if tokens >= 1_000:
        return f"{tokens / 1_000:.1f}k"
    return str(tokens)
