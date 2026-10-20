# Contributing

## Setup
```bash
pip install -e .[dev]
pytest          # must pass before you open a PR
ruff check . && black --check .
```

## Ground rules (from docs/roadmap.md)
- Layers depend downward only: agents -> tools -> gateway -> providers.
- A feature goes in the README only after it has tests and documented behavior.
- No live API calls in tests; use recorded or mocked responses.
- Every module needs a live reference and a test.

## Pull requests
1. Branch from `main`, keep changes focused.
2. Add or update tests and docs, and add a line to `CHANGELOG.md`.
3. CI (pytest, ruff, black) must be green.

## Adding a provider
See `docs/providers.md`. In short: subclass `providers.base.BaseProvider`,
implement `complete`, `stream` (and `embed` if supported), register it in
`providers/__init__.py`, add contract tests with mocked HTTP in
`tests/test_provider_contracts.py`, and give it a pricing entry in
`config/pricing.yml`. `examples/custom_provider.py` is a minimal template.

## Security
Report vulnerabilities as described in `SECURITY.md`, not in public issues.
