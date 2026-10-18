"""Model price table: one source of truth for cost tracking.

Prices live in ``config/pricing.yml`` (USD per million tokens). Providers, the
router and ``utils.cost`` all price through ``get_pricing()``.

A model missing from the table is priced at its provider's ``default`` rate so
requests still work, but it is never silent: the first sighting logs a warning
and every use is counted (``PriceTable.unpriced``, exported as
``caips_unpriced_requests_total``). The set of tracked unpriced models is
bounded.
"""

from __future__ import annotations

import math
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config" / "pricing.yml"
_MAX_UNPRICED_TRACKED = 200
_RATE_FIELDS = {"input", "output", "note"}


class PricingError(ValueError):
    pass


@dataclass(frozen=True)
class Rate:
    input_per_mtok: float
    output_per_mtok: float
    note: str | None = None

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        return (
            max(0, input_tokens or 0) * self.input_per_mtok
            + max(0, output_tokens or 0) * self.output_per_mtok
        ) / 1_000_000


class PriceTable:
    def __init__(
        self,
        providers: dict[str, dict[str, Any]],
        version: int = 1,
        reviewed: str | None = None,
    ):
        # providers: name -> {"default": Rate, "models": {model: Rate}}
        self.providers = providers
        self.version = version
        self.reviewed = reviewed
        self._lock = threading.Lock()
        self.unpriced: dict[tuple[str, str], int] = {}

    def rate(self, provider: str, model: str) -> tuple[Rate | None, bool]:
        """(rate, priced). ``priced`` is False when the default was used;
        the rate is None only for a provider with no table at all."""
        entry = self.providers.get(provider)
        if entry is None:
            return None, False
        rate = entry["models"].get(model)
        if rate is not None:
            return rate, True
        return entry["default"], False

    def cost(
        self,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        *,
        track: bool = True,
    ) -> float:
        """Cost in USD. ``track=False`` is for estimates (such as the router
        comparing providers) so only real billed calls count as unpriced."""
        rate, priced = self.rate(provider, model)
        if rate is None:
            raise PricingError(f"no prices for provider '{provider}'")
        if not priced and track:
            self._note_unpriced(provider, model)
        return rate.cost(input_tokens, output_tokens)

    def _note_unpriced(self, provider: str, model: str) -> None:
        key = (provider, model or "unknown")
        with self._lock:
            if key not in self.unpriced:
                if len(self.unpriced) >= _MAX_UNPRICED_TRACKED:
                    key = (provider, "other")
                if key not in self.unpriced:
                    logger.warning(
                        f"No price for {provider}/{key[1]}; using the "
                        f"{provider} default. Add it to config/pricing.yml."
                    )
            self.unpriced[key] = self.unpriced.get(key, 0) + 1

    def unpriced_stats(self) -> list[dict]:
        with self._lock:
            return [
                {"provider": p, "model": m, "requests": n}
                for (p, m), n in self.unpriced.items()
            ]

    def models(self, provider: str) -> dict[str, Rate]:
        return dict(self.providers.get(provider, {}).get("models", {}))

    def warnings(self) -> list[str]:
        """Entries that look wrong but are allowed (output priced below input)."""
        out = []
        for name, entry in self.providers.items():
            for model, r in entry["models"].items():
                if r.output_per_mtok < r.input_per_mtok:
                    out.append(f"{name}/{model}")
        return sorted(out)

    def per_token(self) -> dict[str, dict[str, dict[str, float]]]:
        """Legacy shape used by ``utils.cost.PRICING``: USD per single token."""
        return {
            name: {
                model: {
                    "input": r.input_per_mtok / 1_000_000,
                    "output": r.output_per_mtok / 1_000_000,
                }
                for model, r in entry["models"].items()
            }
            for name, entry in self.providers.items()
        }


def _parse_rate(raw: Any, where: str, errors: list[str]) -> Rate | None:
    if not isinstance(raw, dict):
        errors.append(f"{where}: must be a mapping with input and output")
        return None
    for key in raw.keys() - _RATE_FIELDS:
        errors.append(f"{where}: unknown field '{key}'")
    values = {}
    for key in ("input", "output"):
        v = raw.get(key)
        if (
            isinstance(v, bool)
            or not isinstance(v, (int, float))
            or not math.isfinite(v)
            or v < 0
        ):
            errors.append(f"{where}.{key}: must be a number >= 0")
        else:
            values[key] = float(v)
    note = raw.get("note")
    if note is not None and not isinstance(note, str):
        errors.append(f"{where}.note: must be text")
        note = None
    if len(values) != 2:
        return None
    return Rate(values["input"], values["output"], note)


def load_pricing(path: str | os.PathLike) -> PriceTable:
    """Load and validate a price file; every problem is reported at once."""
    import yaml

    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError) as exc:
        raise PricingError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise PricingError("top level must be a mapping")

    errors = [
        f"unknown top-level key '{k}'"
        for k in data.keys() - {"version", "reviewed", "providers"}
    ]
    version = data.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        errors.append("version: must be an integer >= 1")
        version = 1
    reviewed = data.get("reviewed")
    if reviewed is not None and not isinstance(reviewed, (str, type(None))):
        reviewed = str(reviewed)  # YAML parses 2026-01-31 as a date

    providers: dict[str, dict[str, Any]] = {}
    raw_providers = data.get("providers")
    if not isinstance(raw_providers, dict) or not raw_providers:
        errors.append("providers: must be a non-empty mapping")
        raw_providers = {}
    for name, body in raw_providers.items():
        where = f"providers.{name}"
        if not isinstance(body, dict):
            errors.append(f"{where}: must be a mapping")
            continue
        for key in body.keys() - {"default", "models"}:
            errors.append(f"{where}: unknown field '{key}'")
        if "default" not in body:
            errors.append(f"{where}: missing 'default' rate")
            default = None
        else:
            default = _parse_rate(body["default"], f"{where}.default", errors)
        models: dict[str, Rate] = {}
        raw_models = body.get("models")
        if not isinstance(raw_models, dict) or not raw_models:
            errors.append(f"{where}.models: must be a non-empty mapping")
            raw_models = {}
        for model, raw in raw_models.items():
            rate = _parse_rate(raw, f"{where}.models.{model}", errors)
            if rate is not None:
                models[str(model)] = rate
        if default is not None:
            providers[str(name)] = {"default": default, "models": models}
    if errors:
        raise PricingError("; ".join(errors))
    table = PriceTable(providers, version=version, reviewed=reviewed)
    return table


_table: PriceTable | None = None
_table_lock = threading.Lock()


def get_pricing() -> PriceTable:
    """The process-wide table (loaded on first use from ``COREAI_PRICING_FILE``
    or ``config/pricing.yml``)."""
    global _table
    if _table is None:
        with _table_lock:
            if _table is None:
                path = os.environ.get("COREAI_PRICING_FILE") or DEFAULT_PATH
                table = load_pricing(path)
                if table.reviewed is None:
                    logger.warning(
                        "Model prices in {} have not been marked as reviewed; "
                        "check them against vendor price pages.",
                        path,
                    )
                for item in table.warnings():
                    logger.warning(f"Price looks wrong (output below input): {item}")
                _table = table
    return _table


def set_pricing(table: PriceTable | None) -> None:
    """Replace the process-wide table (tests, or reloading after an edit)."""
    global _table
    with _table_lock:
        _table = table
