"""Fine-tuning support: cost estimates before launch, durable job records, and
registering a finished model in the router like any other model."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import yaml

MAX_DATASET_BYTES = 100 * 1024 * 1024  # refuse before any paid upload starts


@dataclass(frozen=True)
class TrainingPrices:
    per_mtok: dict[str, float]
    default: float = 8.0
    inference_multiplier: float = 2.0
    reviewed: Optional[str] = None

    def rate(self, base_model: str) -> float:
        # fine-tune ids look like "gpt-4o-mini-2024-07-18": match the longest known prefix
        known = sorted(self.per_mtok, key=len, reverse=True)
        return next(
            (self.per_mtok[k] for k in known if base_model.startswith(k)), self.default
        )


def load_training_prices(path: str | os.PathLike | None = None) -> TrainingPrices:
    p = Path(
        path or os.getenv("COREAI_FINETUNE_PRICING_FILE", "config/finetune_pricing.yml")
    )
    if not p.exists():
        return TrainingPrices({})
    raw = yaml.safe_load(p.read_text()) or {}
    table = dict(raw.get("training") or {})
    default = float(table.pop("default", 8.0))
    return TrainingPrices(
        {k: float(v) for k, v in table.items()},
        default,
        float(raw.get("inference_multiplier", 2.0)),
        raw.get("reviewed"),
    )


def estimate_dataset_tokens(dataset: Any) -> int:
    """~4 characters per token, summed over every message. An estimate, not a tokenizer."""
    chars = sum(
        len(str(m.get("content") or "")) + 4
        for ex in dataset.examples
        for m in ex.messages
    )
    return max(1, chars // 4) if dataset.examples else 0


def estimate_cost(config: Any, prices: TrainingPrices) -> dict:
    tokens = estimate_dataset_tokens(config.dataset)
    billed = tokens * max(1, config.hyperparams.n_epochs)
    rate = prices.rate(config.base_model)
    return {
        "dataset_tokens": tokens,
        "billed_tokens": billed,
        "price_per_mtok": rate,
        "estimated_usd": round(billed * rate / 1_000_000, 4),
        "prices_reviewed": prices.reviewed is not None,
    }


class JobStore:
    """Durable job records so polling resumes after a restart. Stores metadata only,
    never the training data."""

    COLS = (
        "job_id provider base_model suffix provider_job_id status fine_tuned_model trained_tokens "
        "created_at started_at finished_at error price_per_mtok estimated_usd"
    ).split()

    def __init__(self, path: str = ":memory:"):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS ft_jobs (job_id TEXT PRIMARY KEY, provider TEXT, base_model TEXT,"
            " suffix TEXT, provider_job_id TEXT, status TEXT, fine_tuned_model TEXT, trained_tokens INT,"
            " created_at REAL, started_at REAL, finished_at REAL, error TEXT, price_per_mtok REAL,"
            " estimated_usd REAL)"
        )

    def save(self, job: Any) -> None:
        c = job.config
        row = (
            job.job_id, c.provider if c else None, c.base_model if c else None, c.suffix if c else None,
            job.provider_job_id, getattr(job.status, "value", job.status), job.fine_tuned_model,
            job.trained_tokens, job.created_at, job.started_at, job.finished_at, job.error,
            job.price_per_mtok, job.estimated_usd,
        )  # fmt: skip
        self.db.execute(
            f"INSERT OR REPLACE INTO ft_jobs VALUES ({','.join('?' * len(row))})", row
        )
        self.db.commit()

    def load_all(self) -> list[dict]:
        rows = self.db.execute(f"SELECT {','.join(self.COLS)} FROM ft_jobs").fetchall()
        return [dict(zip(self.COLS, r)) for r in rows]


def register_finetuned(
    job: Any, model_registry: Any, price_table: Any, prices: TrainingPrices
) -> bool:
    """Make a finished job's model routable: a catalogue profile cloned from its base model
    and an inference price of base price x ``inference_multiplier``. Returns False if the
    base model is unknown or the job has no model."""
    from dataclasses import replace

    from coreai.pricing import Rate
    from neural.model import TokenPricing

    ft = job.fine_tuned_model
    base = model_registry.get(job.config.base_model) if job.config else None
    if not ft or base is None:
        return False
    m = prices.inference_multiplier
    pricing = (
        TokenPricing(
            base.pricing.input_per_million * m, base.pricing.output_per_million * m
        )
        if base.pricing
        else None
    )
    model_registry.register(
        replace(
            base, model_id=ft, pricing=pricing, notes=f"fine-tuned from {base.model_id}"
        )
    )
    entry = price_table.providers.get(job.config.provider)
    if entry is not None:
        rate, _ = price_table.rate(job.config.provider, job.config.base_model)
        if rate is not None:
            entry["models"][ft] = Rate(
                rate.input_per_mtok * m,
                rate.output_per_mtok * m,
                "fine-tuned; multiplier is an assumption",
            )
    return True
