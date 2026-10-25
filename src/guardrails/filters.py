"""Request and response filters: secrets, PII and blocked topics.

A ``FilterPolicy`` is plain config. ``apply`` returns the (possibly redacted)
text and whether it may pass. Secrets block by default; PII is redacted;
blocked topics are case-insensitive phrases, or regexes written as ``re:...``.
"""

import re
from dataclasses import dataclass, field
from typing import Any

from .pii import ALL_KINDS, DEFAULT_KINDS, detect, redact

SECRET_KINDS = frozenset(
    {"private_key", "api_key", "aws_key", "github_token", "bearer", "jwt"}
)


@dataclass(frozen=True)
class FilterPolicy:
    redact_pii: bool = True
    block_secrets: bool = True
    blocked_topics: tuple[str, ...] = ()
    max_chars: int = 0  # 0 = unlimited

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "FilterPolicy":
        raw = raw or {}
        topics = raw.get("blocked_topics") or []
        if not isinstance(topics, (list, tuple)) or not all(
            isinstance(t, str) for t in topics
        ):
            raise ValueError("blocked_topics must be a list of strings")
        for t in topics:
            if t.startswith("re:"):
                try:
                    re.compile(t[3:])  # fail at load time, not per request
                except re.error as exc:
                    raise ValueError(
                        f"invalid blocked_topics regex {t!r}: {exc}"
                    ) from exc
        return cls(
            redact_pii=bool(raw.get("redact_pii", True)),
            block_secrets=bool(raw.get("block_secrets", True)),
            blocked_topics=tuple(topics),
            max_chars=int(raw.get("max_chars", 0) or 0),
        )


@dataclass
class FilterResult:
    allowed: bool
    text: str
    reasons: list[str] = field(
        default_factory=list
    )  # kinds and topics, never the matched text


def _topic_hit(text: str, topic: str) -> bool:
    if topic.startswith("re:"):
        return re.search(topic[3:], text, re.I) is not None
    return topic.lower() in text.lower()


def apply(text: str, policy: FilterPolicy) -> FilterResult:
    reasons: list[str] = []
    if policy.max_chars and len(text) > policy.max_chars:
        return FilterResult(False, text, [f"too_long:{policy.max_chars}"])
    for topic in policy.blocked_topics:
        if _topic_hit(text, topic):
            reasons.append(f"topic:{topic}")
    if policy.block_secrets:
        kinds = sorted({f.kind for f in detect(text, SECRET_KINDS)})
        reasons += [f"secret:{k}" for k in kinds]
    if reasons:
        return FilterResult(False, text, reasons)
    if policy.redact_pii:
        pii_kinds = {f.kind for f in detect(text, DEFAULT_KINDS)}
        if pii_kinds:
            return FilterResult(
                True,
                redact(text, DEFAULT_KINDS),
                [f"redacted:{k}" for k in sorted(pii_kinds)],
            )
    return FilterResult(True, text, [])


__all__ = ["FilterPolicy", "FilterResult", "apply", "SECRET_KINDS", "ALL_KINDS"]
