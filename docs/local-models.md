# Local models (connect mode)

You run the model server; CaiPS routes to it through the same API as cloud models.
Supported: anything that speaks the OpenAI API (LM Studio, Ollama, `llama-server`, vLLM).

## Setup
1. Copy `config/local.example.yml` to `config/local.yml` and edit it.
2. Start CaiPS. At startup each server is asked for its models (`GET /v1/models`). A server
   that is down is skipped and picked up on the next start; requests to it fail fast
   (2 s connect timeout) and trip the circuit breaker like any provider.
3. Call it: `model: "llama3.2"` goes to the server that lists it.

## Capability flags
Per model: `tools`, `vision`, `json_mode`, `context_length`. Tools and vision are **off by
default**. A request that needs one the model lacks is refused for that server (so the router
can pick another) instead of failing in the model. Oversized prompts are refused the same way.

## Cost
Local calls cost $0 and are recorded with tokens and latency. Set `cost_per_hour_usd` to add an
hardware/electricity estimate (`latency x rate`).

## Routing
- **Private:** send `"metadata": {"private": true}`. The request goes to local servers only; if none can
  serve it, it fails. It is never sent to a cloud provider, including on failover.
- **Local first, cloud fallback:** use a policy fallback chain in `config/routing.yml`, e.g.
  `fallback_chain: [ollama, openai]`. Timeout or a down server fails over to the cloud.
- Not built yet: routing by queue depth and by task difficulty.

## Limits
Streaming works. Tool calls with `stream=true` are buffered by the gateway. Discovery runs at
startup only; restart (or list models in `local.yml`) after loading a new model.
