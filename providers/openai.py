"""
providers/openai.py
"""

import importlib
import queue
import threading
import time
from typing import AsyncGenerator

import tiktoken
from loguru import logger

# importlib guarantees we get the top-level 'openai' SDK package, not this
# submodule (providers.openai) which shares the same short name.
AsyncOpenAI = importlib.import_module("openai").AsyncOpenAI

from .base import BaseProvider, CompletionRequest, CompletionResponse

OPENAI_PRICING = {
    "gpt-4o": {"input": 0.000005, "output": 0.000015},
    "gpt-4o-mini": {"input": 0.00000015, "output": 0.0000006},
    "gpt-4-turbo": {"input": 0.00001, "output": 0.00003},
    "gpt-3.5-turbo": {"input": 0.0000005, "output": 0.0000015},
    "o1": {"input": 0.000015, "output": 0.000060},
    "o1-mini": {"input": 0.000003, "output": 0.000012},
}

# tiktoken.encoding_for_model() downloads its BPE rank file over the network
# on first use if it isn't already cached locally. In offline/sandboxed/
# firewalled environments that download attempt can block for a long time
# before failing. TIKTOKEN_TIMEOUT_S bounds how long we'll wait before
# falling back to the char-based estimate, so this never hangs a request.
TIKTOKEN_TIMEOUT_S = 2.0


class OpenAIProvider(BaseProvider):
    name = "openai"
    default_model = "gpt-4o-mini"

    def __init__(self, api_key: str):
        super().__init__(api_key)
        self.client = AsyncOpenAI(api_key=api_key)

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        model = request.model or self.default_model
        messages = self._build_messages(request)

        start = time.monotonic()
        try:
            response = await self.client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=request.max_tokens,
                temperature=request.temperature,
            )
        except Exception as e:
            logger.error(f"[openai] completion failed: {e}")
            raise

        latency_ms = (time.monotonic() - start) * 1000
        input_tokens = response.usage.prompt_tokens
        output_tokens = response.usage.completion_tokens
        cost = self.estimate_cost(input_tokens, output_tokens, model)
        content = response.choices[0].message.content

        result = CompletionResponse(
            content=content,
            model=model,
            provider=self.name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            latency_ms=latency_ms,
        )
        self._track(result)
        logger.debug(
            f"[openai] {model} | {input_tokens}in {output_tokens}out | "
            f"${cost:.6f} | {latency_ms:.0f}ms"
        )
        return result

    async def stream(self, request: CompletionRequest) -> AsyncGenerator[str, None]:
        model = request.model or self.default_model
        messages = self._build_messages(request)

        try:
            stream = await self.client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=request.max_tokens,
                temperature=request.temperature,
                stream=True,
            )
            async for chunk in stream:
                delta = chunk.choices[0].delta.content
                if delta:
                    yield delta
        except Exception as e:
            logger.error(f"[openai] stream failed: {e}")
            raise

    def estimate_cost(self, input_tokens: int, output_tokens: int, model: str) -> float:
        pricing = OPENAI_PRICING.get(model, {"input": 0.000005, "output": 0.000015})
        return (input_tokens * pricing["input"]) + (output_tokens * pricing["output"])

    def count_tokens(self, text: str, model: str) -> int:
        result_q: queue.Queue = queue.Queue(maxsize=1)

        def _worker():
            try:
                enc = tiktoken.encoding_for_model(model)
                result_q.put(("ok", len(enc.encode(text))))
            except Exception as e:
                result_q.put(("err", e))

        # Daemon thread: if tiktoken really is stuck on a network call, we
        # abandon it rather than block. It'll die with the process instead
        # of hanging this call (or the whole test suite) indefinitely.
        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        t.join(timeout=TIKTOKEN_TIMEOUT_S)

        if t.is_alive():
            logger.warning(
                f"[openai] tiktoken timed out after {TIKTOKEN_TIMEOUT_S}s "
                f"(model={model}) — falling back to char-based estimate"
            )
            return len(text) // 4

        try:
            status, payload = result_q.get_nowait()
        except queue.Empty:
            return len(text) // 4

        if status == "ok":
            return payload
        return len(text) // 4

    def _build_messages(self, request: CompletionRequest) -> list[dict]:
        messages = []
        if request.system_prompt:
            messages.append({"role": "system", "content": request.system_prompt})
        for msg in request.messages:
            messages.append({"role": msg.role, "content": msg.content})
        return messages

    def fine_tuning_client(self) -> "OpenAIFineTuningClient":
        return OpenAIFineTuningClient(self.client)


# OpenAI's status strings observed in the wild vs. neural/training.py's
# TrainingStatus enum. Not identical ("validating_files" vs "validating") --
# mapped explicitly rather than assumed, with a safe fallback for anything
# unrecognized so a future API-side status addition degrades to "still
# running" instead of raising ValueError out of poll().
_OPENAI_STATUS_MAP = {
    "validating_files": "validating",
    "queued": "queued",
    "running": "running",
    "succeeded": "succeeded",
    "failed": "failed",
    "cancelled": "cancelled",
}


class OpenAIFineTuningClient:
    """
    Adapts the real AsyncOpenAI client's fine_tuning.jobs.* API to the
    create_fine_tune/get_fine_tune/cancel_fine_tune interface
    neural/training.py's TrainingManager expects.

    Not a passthrough: OpenAI's real API is a two-step upload-then-create
    flow (you upload a JSONL training file and get a file_id back, THEN
    create the job against that file_id) -- TrainingManager.submit() calls
    create_fine_tune(training_data=<formatted rows>, ...) with the raw
    rows directly. This does the upload step so TrainingManager doesn't
    need to know OpenAI's API shape.
    """

    def __init__(self, client):
        self._client = client

    async def create_fine_tune(
        self, training_data: list[dict], model: str, hyperparameters: dict, suffix: str
    ) -> dict:
        import io
        import json

        jsonl = "\n".join(json.dumps(row) for row in training_data).encode()
        uploaded = await self._client.files.create(
            file=io.BytesIO(jsonl), purpose="fine-tune"
        )
        job = await self._client.fine_tuning.jobs.create(
            training_file=uploaded.id,
            model=model,
            hyperparameters={k: v for k, v in hyperparameters.items() if v is not None},
            suffix=suffix[:18] if suffix else None,  # API-enforced suffix length limit
        )
        return {"id": job.id}

    async def get_fine_tune(self, provider_job_id: str) -> dict:
        job = await self._client.fine_tuning.jobs.retrieve(provider_job_id)
        mapped_status = _OPENAI_STATUS_MAP.get(job.status)
        if mapped_status is None:
            logger.warning(
                f"[openai] unrecognized fine-tune status '{job.status}' for "
                f"job {provider_job_id} — treating as still running"
            )
            mapped_status = "running"
        result = {
            "status": mapped_status,
            "trained_tokens": getattr(job, "trained_tokens", None) or 0,
            "fine_tuned_model": job.fine_tuned_model,
        }
        if mapped_status == "failed" and getattr(job, "error", None):
            result["error"] = {"message": job.error.message}
        return result

    async def cancel_fine_tune(self, provider_job_id: str) -> None:
        await self._client.fine_tuning.jobs.cancel(provider_job_id)
