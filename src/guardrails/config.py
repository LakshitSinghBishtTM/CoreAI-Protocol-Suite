"""Load the filter policy from ``config/guardrails.yml`` (or ``COREAI_GUARDRAILS_CONFIG``).

No file means no filtering, so existing deployments behave as before. Example::

    input:  {blocked_topics: ["re:\\\\bcredit card dump\\\\b"], block_secrets: true, redact_pii: true}
    output: {block_secrets: true, redact_pii: true}
"""

import os
from pathlib import Path

import yaml

from .filters import FilterPolicy

_cache: dict[str, tuple[FilterPolicy | None, FilterPolicy | None]] = {}


def load(path: str | None = None) -> tuple[FilterPolicy | None, FilterPolicy | None]:
    """Return ``(input_policy, output_policy)``; either may be None."""
    p = Path(path or os.environ.get("COREAI_GUARDRAILS_CONFIG") or "config/guardrails.yml")
    key = str(p)
    if key not in _cache:
        if not p.is_file():
            _cache[key] = (None, None)
        else:
            raw = yaml.safe_load(p.read_text()) or {}
            _cache[key] = tuple(  # type: ignore[assignment]
                FilterPolicy.from_dict(raw[s]) if s in raw else None for s in ("input", "output")
            )
    return _cache[key]


def reset() -> None:
    _cache.clear()
