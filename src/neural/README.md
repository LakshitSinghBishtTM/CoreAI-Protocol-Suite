# neural/

The package name is historical. It holds two unrelated things:

- `model.py`: the **model registry**. Capability profiles, context windows, token prices and the provider-to-model mapping. The router uses it for context-window checks and `GET /v1/models` serves it. It is a static table, so entries (and prices) go stale as providers release models. Keeping it current is a known gap.
- `training.py`: **fine-tuning** job submission and tracking, served under `/v1/finetune/jobs`. OpenAI only; the Anthropic path is a stub. This is an optional feature that will be hardened and moved behind its own install extra.

Both will likely move to clearer package names (for example `models` and `training`) when the repo adopts a `src/` layout.
