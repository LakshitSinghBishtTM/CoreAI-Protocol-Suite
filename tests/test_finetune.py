import types

import pytest

from coreai.pricing import PriceTable, Rate
from neural.finetune import (
    JobStore,
    TrainingPrices,
    estimate_cost,
    load_training_prices,
    register_finetuned,
)
from neural.model import ModelRegistry, get_registry
from neural.training import (
    DatasetValidationError,
    HyperparamConfig,
    TrainingDataset,
    TrainingError,
    TrainingExample,
    TrainingJobConfig,
    TrainingManager,
    TrainingStatus,
    UnsupportedProviderError,
)
from providers.openai import OpenAIFineTuningClient

PRICES = TrainingPrices({"gpt-4o-mini": 3.0}, default=8.0, inference_multiplier=2.0)


def ex(u="hello there", a="hi back"):
    return TrainingExample(
        [{"role": "user", "content": u}, {"role": "assistant", "content": a}]
    )


def cfg(n=12, epochs=3, base="gpt-4o-mini", provider="openai", text="x" * 400):
    return TrainingJobConfig(
        base, provider, TrainingDataset(examples=[ex(text, text) for _ in range(n)]),
        hyperparams=HyperparamConfig(n_epochs=epochs),
    )  # fmt: skip


class FakeClient:
    def __init__(self, *states):
        self.states, self.created, self.cancelled = list(states), [], []

    async def create_fine_tune(self, **kw):
        self.created.append(kw)
        return {"id": "ftjob-prov-1"}

    async def get_fine_tune(self, pid):
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]

    async def cancel_fine_tune(self, pid):
        self.cancelled.append(pid)


# ------------------------------------------------------------- validation


def test_example_validation_rules():
    assert ex().validate() == []
    bad = TrainingExample(
        [{"role": "robot", "content": " "}, {"role": "assistant", "content": "x"}]
    )
    errs = " ".join(bad.validate())
    assert "unknown role" in errs and "non-empty" in errs and "'user'" in errs


def test_dataset_validation_and_formatter():
    assert TrainingDataset().validate()["valid"] is False
    from neural.training import DatasetFormatter

    rows = DatasetFormatter().format(cfg(1).dataset, "openai")
    assert rows[0]["messages"][0]["role"] == "user"
    with pytest.raises(UnsupportedProviderError):
        DatasetFormatter().format(cfg(1).dataset, "gemini")


# -------------------------------------------------------------- estimates


def test_estimate_scales_with_epochs_and_uses_model_price():
    a = estimate_cost(cfg(epochs=1), PRICES)
    b = estimate_cost(cfg(epochs=4), PRICES)
    assert b["billed_tokens"] == 4 * a["billed_tokens"]
    assert a["price_per_mtok"] == 3.0
    assert (
        estimate_cost(cfg(base="gpt-4o-mini-2024-07-18"), PRICES)["price_per_mtok"]
        == 3.0
    )
    assert estimate_cost(cfg(base="unknown-model"), PRICES)["price_per_mtok"] == 8.0
    assert a["estimated_usd"] == pytest.approx(a["billed_tokens"] * 3 / 1e6, abs=1e-4)


def test_price_file_loads_and_flags_unreviewed(tmp_path):
    f = tmp_path / "p.yml"
    f.write_text(
        "reviewed: null\ninference_multiplier: 3\ntraining: {default: 9, gpt-4o: 25}"
    )
    p = load_training_prices(f)
    assert p.rate("gpt-4o") == 25 and p.rate("x") == 9 and p.inference_multiplier == 3
    assert estimate_cost(cfg(), p)["prices_reviewed"] is False
    assert load_training_prices(tmp_path / "missing.yml").per_mtok == {}
    assert (
        load_training_prices("config/finetune_pricing.yml").rate("gpt-4o-mini") == 3.0
    )


# ------------------------------------------------------------ job lifecycle


@pytest.mark.asyncio
async def test_cost_limit_blocks_before_any_upload():
    m = TrainingManager(prices=PRICES)
    client = FakeClient({"status": "queued"})
    est = m.estimate(cfg())["estimated_usd"]
    with pytest.raises(TrainingError, match="exceeds the limit"):
        await m.submit(cfg(), client, max_cost_usd=est / 2)
    assert client.created == [] and m.list_jobs() == []
    job = await m.submit(cfg(), client, max_cost_usd=est * 2)
    assert job.estimated_usd == est and len(client.created) == 1


@pytest.mark.asyncio
async def test_invalid_dataset_never_reaches_provider():
    m = TrainingManager(prices=PRICES)
    c = cfg()
    c.dataset.examples[0].messages[-1]["role"] = "user"
    client = FakeClient({"status": "queued"})
    with pytest.raises(DatasetValidationError):
        await m.submit(c, client)
    assert client.created == []


@pytest.mark.asyncio
async def test_poll_to_success_fires_hook_once_and_reports_actual_cost():
    seen = []
    m = TrainingManager(prices=PRICES, on_succeeded=seen.append)
    client = FakeClient(
        {"status": "running"},
        {
            "status": "succeeded",
            "trained_tokens": 2_000_000,
            "fine_tuned_model": "ft:gpt-4o-mini:acme::abc",
        },
    )
    job = await m.submit(cfg(), client)
    await m.poll(job.job_id, client)
    assert job.status == TrainingStatus.RUNNING and seen == []
    await m.poll(job.job_id, client)
    await m.poll(job.job_id, client)  # polling a finished job again must not re-fire
    assert seen == [job] and job.fine_tuned_model.startswith("ft:")
    assert job.cost_estimate_usd == pytest.approx(6.0)  # 2M tokens x $3/M


@pytest.mark.asyncio
async def test_hook_failure_does_not_lose_job_state():
    def boom(job):
        raise RuntimeError("registry down")

    m = TrainingManager(prices=PRICES, on_succeeded=boom)
    client = FakeClient(
        {"status": "succeeded", "fine_tuned_model": "ft:x", "trained_tokens": 1}
    )
    job = await m.submit(cfg(), client)
    await m.poll(job.job_id, client)
    assert job.status == TrainingStatus.SUCCEEDED and job.fine_tuned_model == "ft:x"


@pytest.mark.asyncio
async def test_cancel_rules():
    m = TrainingManager(prices=PRICES)
    client = FakeClient({"status": "queued"})
    job = await m.submit(cfg(), client)
    await m.cancel(job.job_id, client)
    assert job.status == TrainingStatus.CANCELLED and client.cancelled == [
        "ftjob-prov-1"
    ]
    with pytest.raises(TrainingError):
        await m.cancel(job.job_id, client)


@pytest.mark.asyncio
async def test_submit_failure_is_recorded():
    class Down(FakeClient):
        async def create_fine_tune(self, **kw):
            raise ConnectionError("no route")

    m = TrainingManager(prices=PRICES, store=JobStore())
    with pytest.raises(TrainingError, match="no route"):
        await m.submit(cfg(), Down())
    assert m.list_jobs()[0].status == TrainingStatus.FAILED


# -------------------------------------------------------------- persistence


@pytest.mark.asyncio
async def test_jobs_survive_restart_and_resume_polling(tmp_path):
    db = str(tmp_path / "ft.db")
    m1 = TrainingManager(prices=PRICES, store=JobStore(db))
    client = FakeClient({"status": "running"})
    job = await m1.submit(cfg(), client)
    m2 = TrainingManager(prices=PRICES, store=JobStore(db))  # "restart"
    restored = m2.get_job(job.job_id)
    assert (
        restored.status == TrainingStatus.QUEUED
        and restored.provider_job_id == "ftjob-prov-1"
    )
    assert (
        restored.estimated_usd == job.estimated_usd
        and restored.config.base_model == "gpt-4o-mini"
    )
    await m2.poll(job.job_id, client)
    assert JobStore(db).load_all()[0]["status"] == "running"
    assert "dataset" not in str(JobStore(db).load_all()[0]).lower()  # metadata only


# ----------------------------------------------------- router registration


def test_register_finetuned_makes_model_routable_with_priced_inference():
    reg = ModelRegistry(dict(get_registry()._catalogue))
    base = reg.get("gpt-4o-mini")
    assert base is not None
    table = PriceTable(
        {"openai": {"default": Rate(5, 15), "models": {"gpt-4o-mini": Rate(0.15, 0.6)}}}
    )
    job = types.SimpleNamespace(
        fine_tuned_model="ft:gpt-4o-mini:acme::abc", config=cfg()
    )
    assert register_finetuned(job, reg, table, PRICES) is True
    p = reg.get("ft:gpt-4o-mini:acme::abc")
    assert p.provider == "openai" and p.context_window == base.context_window
    assert p.pricing.input_per_million == pytest.approx(
        base.pricing.input_per_million * 2
    )
    rate, priced = table.rate("openai", "ft:gpt-4o-mini:acme::abc")
    assert priced and rate.output_per_mtok == pytest.approx(1.2)
    assert reg.get("gpt-4o-mini") is base  # base model untouched


def test_register_skips_unknown_base_or_missing_model():
    reg = ModelRegistry(dict(get_registry()._catalogue))
    t = PriceTable({})
    assert (
        register_finetuned(
            types.SimpleNamespace(fine_tuned_model=None, config=cfg()), reg, t, PRICES
        )
        is False
    )
    j = types.SimpleNamespace(fine_tuned_model="ft:x", config=cfg(base="no-such-model"))
    assert register_finetuned(j, reg, t, PRICES) is False


# ---------------------------------------------- OpenAI adapter (mocked SDK)


@pytest.mark.asyncio
async def test_openai_adapter_uploads_then_creates_and_maps_status():
    calls = {}

    async def files_create(file, purpose):
        calls["upload"] = (file.read().decode(), purpose)
        return types.SimpleNamespace(id="file-1")

    async def jobs_create(**kw):
        calls["create"] = kw
        return types.SimpleNamespace(id="ftjob-9")

    async def jobs_retrieve(pid):
        return types.SimpleNamespace(
            status="validating_files",
            trained_tokens=None,
            fine_tuned_model=None,
            error=None,
        )

    sdk = types.SimpleNamespace(
        files=types.SimpleNamespace(create=files_create),
        fine_tuning=types.SimpleNamespace(
            jobs=types.SimpleNamespace(
                create=jobs_create, retrieve=jobs_retrieve, cancel=None
            )
        ),
    )
    c = OpenAIFineTuningClient(sdk)
    r = await c.create_fine_tune(
        [{"messages": [{"role": "user", "content": "a"}]}], "gpt-4o-mini",
        {"n_epochs": 2, "batch_size": None}, "my-suffix-that-is-way-too-long",
    )  # fmt: skip
    assert r == {"id": "ftjob-9"} and calls["upload"][1] == "fine-tune"
    assert calls["create"]["training_file"] == "file-1"
    assert calls["create"]["hyperparameters"] == {"n_epochs": 2}  # None dropped
    assert len(calls["create"]["suffix"]) <= 18
    assert (await c.get_fine_tune("ftjob-9"))["status"] == "validating"


def test_default_manager_wiring(monkeypatch, tmp_path):
    import neural.training as t

    monkeypatch.setattr(t, "_manager", None)
    monkeypatch.setenv("COREAI_FINETUNE_DB", str(tmp_path / "x.db"))
    m = t.get_training_manager()
    assert m.store is not None and m.on_succeeded is not None and m.prices.rate("gpt-4o-mini") == 3.0
    monkeypatch.setattr(t, "_manager", None)
