"""Load the filter policy from ``config/guardrails.yml`` (or ``COREAI_GUARDRAILS_CONFIG``).

No file means no filtering, so existing deployments behave as before. Example::

    input:  {blocked_topics: ["re:\\\\bcredit card dump\\\\b"], block_secrets: true, redact_pii: true}
    output: {block_secrets: true, redact_pii: true}
"""

import fnmatch
import os
from pathlib import Path

import yaml

from .filters import FilterPolicy

_raw_cache: dict[str, dict] = {}
_subj_cache: dict[str, FilterPolicy] = {}
_cache: dict[str, tuple[FilterPolicy | None, FilterPolicy | None]] = {}


def load(path: str | None = None) -> tuple[FilterPolicy | None, FilterPolicy | None]:
    """Return ``(input_policy, output_policy)``; either may be None."""
    p = Path(
        path or os.environ.get("COREAI_GUARDRAILS_CONFIG") or "config/guardrails.yml"
    )
    key = str(p)
    if key not in _cache:
        if not p.is_file():
            _cache[key] = (None, None)
        else:
            raw = yaml.safe_load(p.read_text()) or {}
            _cache[key] = tuple(  # type: ignore[assignment]
                FilterPolicy.from_dict(raw[s]) if s in raw else None
                for s in ("input", "output")
            )
    return _cache[key]


def _raw(path: str | None = None) -> dict:
    p = Path(
        path or os.environ.get("COREAI_GUARDRAILS_CONFIG") or "config/guardrails.yml"
    )
    key = "raw:" + str(p)
    if key not in _raw_cache:
        _raw_cache[key] = (yaml.safe_load(p.read_text()) or {}) if p.is_file() else {}
    return _raw_cache[key]


def for_subject(
    subject: str | None, path: str | None = None
) -> tuple[FilterPolicy | None, FilterPolicy | None]:
    """``(input, output)`` policy for one key or user.

    A ``subjects.<name>`` block replaces the global ``input``/``output`` section
    it defines; sections it omits fall back to the global policy.
    """
    base = load(path)
    over = (_raw(path).get("subjects") or {}).get(subject or "") or {}
    out: list[FilterPolicy | None] = []
    for i, sec in enumerate(("input", "output")):
        if sec in over:
            ck = f"subj:{subject}:{sec}:{path}"
            if ck not in _subj_cache:
                _subj_cache[ck] = FilterPolicy.from_dict(over[sec])
            out.append(_subj_cache[ck])
        else:
            out.append(base[i])
    return out[0], out[1]


def model_allowed(subject: str | None, model: str, path: str | None = None) -> bool:
    """False when ``subjects.<name>.allowed_models`` exists and has no glob matching ``model``."""
    over = (_raw(path).get("subjects") or {}).get(subject or "") or {}
    pats = over.get("allowed_models")
    if pats is None:
        return True
    return any(fnmatch.fnmatchcase(model, str(pt)) for pt in pats)


def reset() -> None:
    _raw_cache.clear()
    _subj_cache.clear()
    _cache.clear()
