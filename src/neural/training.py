"""
neural/training.py

CoreAI Neural Training Adapter.
Provides hooks for fine-tuning job submission, dataset formatting,
training run monitoring, and checkpoint management across supported
provider fine-tuning APIs (OpenAI, Anthropic planned).
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class TrainingStatus(str, Enum):
    QUEUED = "queued"
    VALIDATING = "validating"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class DatasetFormat(str, Enum):
    CHAT = "chat"  # {"messages": [...]} per line
    COMPLETION = "completion"  # {"prompt": ..., "completion": ...}
    PREFERENCE = "preference"  # {"chosen": ..., "rejected": ...} for DPO


class HyperparamPreset(str, Enum):
    CONSERVATIVE = "conservative"  # low LR, high epochs — safest
    BALANCED = "balanced"  # default recommendation
    AGGRESSIVE = "aggressive"  # higher LR, fewer epochs — faster


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class TrainingExample:
    messages: list[dict]  # [{"role": ..., "content": ...}]
    weight: float = 1.0  # per-example loss weight

    def validate(self) -> list[str]:
        errors = []
        if not self.messages:
            errors.append("messages list is empty")
        roles = [m.get("role") for m in self.messages]
        if roles and roles[-1] != "assistant":
            errors.append("last message must be from 'assistant'")
        if "system" in roles and roles.index("system") != 0:
            errors.append("system message must be first")
        for m in self.messages:
            if m.get("role") not in ("system", "user", "assistant", "tool"):
                errors.append(f"unknown role {m.get('role')!r}")
            if not isinstance(m.get("content"), str) or not m["content"].strip():
                errors.append("every message needs non-empty text content")
        if "user" not in roles:
            errors.append("example needs at least one 'user' message")
        return errors


@dataclass
class TrainingDataset:
    dataset_id: str = field(default_factory=lambda: str(uuid.uuid4())[:12])
    format: DatasetFormat = DatasetFormat.CHAT
    examples: list[TrainingExample] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    description: str = ""

    def validate(self) -> dict:
        if not self.examples:
            return {"valid": False, "errors": ["dataset is empty"], "warnings": []}

        errors: list[str] = []
        warnings: list[str] = []

        for i, ex in enumerate(self.examples):
            ex_errors = ex.validate()
            for err in ex_errors:
                errors.append(f"example[{i}]: {err}")

        if len(self.examples) < 10:
            warnings.append(
                f"Only {len(self.examples)} examples — recommended minimum is 50"
            )
        if len(self.examples) > 50_000:
            warnings.append(
                "Large dataset (>50k examples) — training may take several hours"
            )

        return {
            "valid": len(errors) == 0,
            "example_count": len(self.examples),
            "errors": errors,
            "warnings": warnings,
        }

    def split(
        self, train_ratio: float = 0.9
    ) -> tuple["TrainingDataset", "TrainingDataset"]:
        split_idx = int(len(self.examples) * train_ratio)
        train = TrainingDataset(format=self.format, examples=self.examples[:split_idx])
        val = TrainingDataset(format=self.format, examples=self.examples[split_idx:])
        return train, val

    def summary(self) -> dict:
        return {
            "dataset_id": self.dataset_id,
            "format": self.format,
            "example_count": len(self.examples),
            "description": self.description,
        }


@dataclass
class HyperparamConfig:
    n_epochs: int = 3
    learning_rate_multiplier: float = 1.0
    batch_size: int = 4
    warmup_steps: int = 50
    weight_decay: float = 0.01
    gradient_clipping: float = 1.0
    seed: int = 42

    @classmethod
    def from_preset(cls, preset: HyperparamPreset) -> "HyperparamConfig":
        if preset == HyperparamPreset.CONSERVATIVE:
            return cls(
                n_epochs=5, learning_rate_multiplier=0.5, batch_size=2, warmup_steps=100
            )
        elif preset == HyperparamPreset.AGGRESSIVE:
            return cls(
                n_epochs=2, learning_rate_multiplier=2.0, batch_size=8, warmup_steps=20
            )
        return cls()  # BALANCED defaults


@dataclass
class TrainingJobConfig:
    base_model: str
    provider: str
    dataset: TrainingDataset
    hyperparams: HyperparamConfig = field(default_factory=HyperparamConfig)
    suffix: str = "coreai-ft"
    description: str = ""
    tags: list[str] = field(default_factory=list)


@dataclass
class TrainingCheckpoint:
    checkpoint_id: str
    step: int
    train_loss: float
    val_loss: Optional[float]
    created_at: float = field(default_factory=time.time)


@dataclass
class TrainingJob:
    job_id: str = field(default_factory=lambda: f"ftjob-{uuid.uuid4().hex[:16]}")
    config: Optional[TrainingJobConfig] = None
    status: TrainingStatus = TrainingStatus.QUEUED
    fine_tuned_model: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    trained_tokens: int = 0
    checkpoints: list[TrainingCheckpoint] = field(default_factory=list)
    error: Optional[str] = None
    provider_job_id: Optional[str] = None
    price_per_mtok: float = (
        8.0  # $ per 1M trained tokens, set at submit from the price file
    )
    estimated_usd: Optional[float] = None  # pre-launch estimate

    @property
    def duration_s(self) -> Optional[float]:
        if self.started_at is not None and self.finished_at is not None:
            return round(self.finished_at - self.started_at, 1)
        return None

    @property
    def cost_estimate_usd(self) -> float:
        """Actual cost so far: provider-reported trained tokens x the job's price."""
        return round(self.trained_tokens * self.price_per_mtok / 1_000_000, 4)

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "fine_tuned_model": self.fine_tuned_model,
            "base_model": self.config.base_model if self.config else None,
            "provider": self.config.provider if self.config else None,
            "trained_tokens": self.trained_tokens,
            "duration_s": self.duration_s,
            "cost_estimate_usd": self.cost_estimate_usd,
            "estimated_usd": self.estimated_usd,
            "checkpoint_count": len(self.checkpoints),
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class TrainingError(RuntimeError):
    pass


class DatasetValidationError(TrainingError):
    pass


class UnsupportedProviderError(TrainingError):
    pass


# ---------------------------------------------------------------------------
# Dataset formatter
# ---------------------------------------------------------------------------


class DatasetFormatter:
    """
    Converts raw training data into provider-specific JSONL upload format.
    """

    SUPPORTED_PROVIDERS = {"openai", "anthropic"}

    def format(self, dataset: TrainingDataset, provider: str) -> list[dict]:
        if provider not in self.SUPPORTED_PROVIDERS:
            raise UnsupportedProviderError(
                f"Fine-tuning not supported for provider '{provider}'. "
                f"Supported: {self.SUPPORTED_PROVIDERS}"
            )
        if provider == "openai":
            return self._format_openai(dataset)
        elif provider == "anthropic":
            return self._format_anthropic(dataset)
        return []

    def _format_openai(self, dataset: TrainingDataset) -> list[dict]:
        rows = []
        for ex in dataset.examples:
            row: dict[str, Any] = {"messages": ex.messages}
            if ex.weight != 1.0:
                row["weight"] = ex.weight
            rows.append(row)
        return rows

    def _format_anthropic(self, dataset: TrainingDataset) -> list[dict]:
        # Anthropic fine-tune format (Messages API compatible)
        rows = []
        for ex in dataset.examples:
            system = next(
                (m["content"] for m in ex.messages if m["role"] == "system"), None
            )
            turns = [m for m in ex.messages if m["role"] != "system"]
            row: dict[str, Any] = {"messages": turns}
            if system:
                row["system"] = system
            rows.append(row)
        return rows


# ---------------------------------------------------------------------------
# Training manager
# ---------------------------------------------------------------------------


class TrainingManager:
    """
    Manages fine-tuning job lifecycle: submit, monitor, cancel, list.
    In production, provider_client is an actual API client instance.
    Here it accepts any object with a .create_fine_tune() coroutine.
    """

    def __init__(self, store=None, prices=None, on_succeeded=None):
        from neural.finetune import TrainingPrices

        self._jobs: dict[str, TrainingJob] = {}
        self._formatter = DatasetFormatter()
        self.store = store  # optional JobStore: jobs survive restarts
        self.prices = prices or TrainingPrices({})
        self.on_succeeded = on_succeeded  # called once with the job when it succeeds
        for r in store.load_all() if store else []:
            self._restore(r)

    def _restore(self, r: dict) -> None:
        cfg = TrainingJobConfig(
            r["base_model"], r["provider"], TrainingDataset(), suffix=r["suffix"] or ""
        )
        j = TrainingJob(
            job_id=r["job_id"], config=cfg, status=TrainingStatus(r["status"])
        )
        for k in ("provider_job_id", "fine_tuned_model", "trained_tokens", "created_at", "started_at",
                  "finished_at", "error", "price_per_mtok", "estimated_usd"):  # fmt: skip
            setattr(j, k, r[k])
        self._jobs[j.job_id] = j

    def _save(self, job: TrainingJob) -> None:
        if self.store:
            self.store.save(job)

    def estimate(self, config: TrainingJobConfig) -> dict:
        from neural.finetune import estimate_cost

        return estimate_cost(config, self.prices)

    async def submit(
        self,
        config: TrainingJobConfig,
        provider_client: Any,
        max_cost_usd: Optional[float] = None,
    ) -> TrainingJob:
        """Validate, estimate the cost, and refuse (before anything is uploaded) if the estimate
        exceeds ``max_cost_usd``."""
        from neural.finetune import MAX_DATASET_BYTES

        validation = config.dataset.validate()
        if not validation["valid"]:
            raise DatasetValidationError(
                f"Dataset validation failed: {validation['errors']}"
            )
        if validation["warnings"]:
            for w in validation["warnings"]:
                logger.warning("Dataset warning: %s", w)

        formatted = self._formatter.format(config.dataset, config.provider)
        if sum(len(str(r)) for r in formatted) > MAX_DATASET_BYTES:
            raise DatasetValidationError("Dataset exceeds the 100 MB upload limit.")
        est = self.estimate(config)
        if max_cost_usd is not None and est["estimated_usd"] > max_cost_usd:
            raise TrainingError(
                f"Estimated cost ${est['estimated_usd']} exceeds the limit ${max_cost_usd}; nothing was submitted."
            )

        job = TrainingJob(
            config=config,
            price_per_mtok=est["price_per_mtok"],
            estimated_usd=est["estimated_usd"],
        )
        self._jobs[job.job_id] = job

        try:
            logger.info(
                "Submitting fine-tune job %s: base=%s provider=%s examples=%d",
                job.job_id,
                config.base_model,
                config.provider,
                len(formatted),
            )
            provider_response = await provider_client.create_fine_tune(
                training_data=formatted,
                model=config.base_model,
                hyperparameters={
                    "n_epochs": config.hyperparams.n_epochs,
                    "batch_size": config.hyperparams.batch_size,
                    "learning_rate_multiplier": config.hyperparams.learning_rate_multiplier,
                },
                suffix=config.suffix,
            )
            job.provider_job_id = provider_response.get("id")
            job.status = TrainingStatus.QUEUED
            logger.info(
                "Job %s submitted → provider ID: %s", job.job_id, job.provider_job_id
            )

        except Exception as exc:
            job.status = TrainingStatus.FAILED
            job.error = str(exc)
            self._save(job)
            raise TrainingError(f"Failed to submit job {job.job_id}: {exc}") from exc

        self._save(job)
        return job

    async def poll(self, job_id: str, provider_client: Any) -> TrainingJob:
        job = self._get_or_raise(job_id)
        if job.provider_job_id is None:
            raise TrainingError(f"Job {job_id} has no provider job ID")

        raw = await provider_client.get_fine_tune(job.provider_job_id)
        previous = job.status
        was_done = previous in (
            TrainingStatus.SUCCEEDED,
            TrainingStatus.FAILED,
            TrainingStatus.CANCELLED,
        )
        job.status = TrainingStatus(raw.get("status", "queued"))
        if job.started_at is None and (
            job.status == TrainingStatus.RUNNING
            or (
                job.status
                in (
                    TrainingStatus.SUCCEEDED,
                    TrainingStatus.FAILED,
                )
                and previous != TrainingStatus.QUEUED
            )
        ):
            # First time we see it running: prefer the provider's own timestamp.
            ts = raw.get("started_at") or raw.get("started")
            job.started_at = float(ts) if isinstance(ts, (int, float)) else time.time()
        job.trained_tokens = raw.get("trained_tokens", 0)
        job.fine_tuned_model = raw.get("fine_tuned_model")

        if job.status in (
            TrainingStatus.SUCCEEDED,
            TrainingStatus.FAILED,
            TrainingStatus.CANCELLED,
        ):
            job.finished_at = time.time()
            if job.status == TrainingStatus.FAILED:
                job.error = raw.get("error", {}).get("message", "Unknown error")
                logger.error("Training job %s failed: %s", job_id, job.error)
            else:
                logger.info(
                    "Training job %s → status=%s model=%s",
                    job_id,
                    job.status,
                    job.fine_tuned_model,
                )
            if (
                job.status == TrainingStatus.SUCCEEDED
                and not was_done
                and self.on_succeeded
            ):
                try:
                    self.on_succeeded(job)
                except Exception:  # a registration problem must not lose the job state
                    logger.exception("on_succeeded hook failed for %s", job_id)
        self._save(job)
        return job

    async def cancel(self, job_id: str, provider_client: Any) -> TrainingJob:
        job = self._get_or_raise(job_id)
        if job.status not in (TrainingStatus.QUEUED, TrainingStatus.RUNNING):
            raise TrainingError(f"Cannot cancel job in state {job.status}")
        await provider_client.cancel_fine_tune(job.provider_job_id)
        job.status = TrainingStatus.CANCELLED
        job.finished_at = time.time()
        self._save(job)
        logger.info("Training job %s cancelled", job_id)
        return job

    def list_jobs(self, status: Optional[TrainingStatus] = None) -> list[TrainingJob]:
        jobs = list(self._jobs.values())
        if status:
            jobs = [j for j in jobs if j.status == status]
        return sorted(jobs, key=lambda j: j.created_at, reverse=True)

    def get_job(self, job_id: str) -> Optional[TrainingJob]:
        return self._jobs.get(job_id)

    def _get_or_raise(self, job_id: str) -> TrainingJob:
        job = self._jobs.get(job_id)
        if not job:
            raise KeyError(f"Unknown training job: {job_id}")
        return job

    def summary(self) -> dict:
        jobs = list(self._jobs.values())
        return {
            "total_jobs": len(jobs),
            "by_status": {
                s.value: sum(1 for j in jobs if j.status == s) for s in TrainingStatus
            },
            "total_trained_tokens": sum(j.trained_tokens for j in jobs),
            "estimated_total_cost_usd": round(
                sum(j.cost_estimate_usd for j in jobs), 4
            ),
        }


_manager: Optional[TrainingManager] = None


def get_training_manager() -> TrainingManager:
    """Process-wide manager: prices from config/finetune_pricing.yml, jobs persisted when
    ``COREAI_FINETUNE_DB`` is set, and finished models registered in the model catalogue.
    """
    global _manager
    if _manager is None:
        import os

        from neural.finetune import JobStore, load_training_prices, register_finetuned

        prices = load_training_prices()

        def _register(job: TrainingJob) -> None:
            from coreai.pricing import get_pricing
            from neural.model import get_registry

            ok = register_finetuned(job, get_registry(), get_pricing(), prices)
            logger.info("Fine-tuned model %s registered: %s", job.fine_tuned_model, ok)

        db = os.getenv("COREAI_FINETUNE_DB")
        _manager = TrainingManager(JobStore(db) if db else None, prices, _register)
    return _manager
