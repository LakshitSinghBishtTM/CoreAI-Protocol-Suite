"""Regex PII and secret detection. Fast, dependency-free, deliberately conservative:
it catches common formats, not every possible leak."""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Finding:
    kind: str
    start: int
    end: int


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        total, alt = total + d, not alt
    return total % 10 == 0


def _card(m: re.Match) -> bool:
    d = re.sub(r"\D", "", m.group())
    return 13 <= len(d) <= 19 and _luhn(d)


# (kind, pattern, optional validator). Secrets first: they win overlaps.
_RULES: list[tuple[str, re.Pattern, object]] = [
    ("private_key", re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)",
        re.S,
    ), None),
    ("api_key", re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|sk-ant-[A-Za-z0-9_-]{16,}|AIza[0-9A-Za-z_-]{30,})"), None),
    ("aws_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), None),
    ("github_token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}"), None),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"), None),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), None),
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), None),
    ("ssn", re.compile(r"\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b"), None),
    ("credit_card", re.compile(r"\b(?:\d[ -]?){12,18}\d\b"), _card),
    ("phone", re.compile(r"(?<![\w.])(?:\+\d{1,3}[ .-]?)?(?:\(\d{3}\)\s?|\d{3}[ .-])\d{3}[ .-]\d{4}\b"), None),
    ("ipv4", re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b"), None),
]  # fmt: skip

ALL_KINDS = frozenset(k for k, _, _ in _RULES)
# Kinds on by default for logs: secrets and direct identifiers. IPs are left alone
# because operators need them for debugging; opt in with kinds=ALL_KINDS.
DEFAULT_KINDS = ALL_KINDS - {"ipv4"}


def detect(
    text: str, kinds: frozenset[str] | set[str] = DEFAULT_KINDS
) -> list[Finding]:
    found: list[Finding] = []
    for kind, pat, check in _RULES:
        if kind not in kinds:
            continue
        for m in pat.finditer(text):
            if check and not check(m):
                continue
            if any(m.start() < f.end and f.start < m.end() for f in found):
                continue  # overlaps an earlier (higher priority) finding
            found.append(Finding(kind, m.start(), m.end()))
    return sorted(found, key=lambda f: f.start)


def redact(text: str, kinds: frozenset[str] | set[str] = DEFAULT_KINDS) -> str:
    """Replace each finding with ``[REDACTED:kind]``."""
    if not text:
        return text
    out, pos = [], 0
    for f in detect(text, kinds):
        out += [text[pos : f.start], f"[REDACTED:{f.kind}]"]
        pos = f.end
    out.append(text[pos:])
    return "".join(out)
