# Examples

Run the gateway first (`uvicorn api.server:app`), then:

| File | Shows |
| --- | --- |
| `basic_routing.py` | OpenAI SDK against CaiPS, using the `fast` / `cheap` / `best` aliases |
| `fallback_chain.yml` | A routing policy with a per-policy fallback chain |
| `budgets.yml` | Per-subject and per-model spend limits |
| `custom_provider.py` | Minimal custom provider on `BaseProvider` |
