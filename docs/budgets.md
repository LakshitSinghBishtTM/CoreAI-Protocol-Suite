# Budgets and quotas

Limit what each API subject (the JWT `sub`) can use per window: spend,
requests and tokens. Off by default; create `config/budgets.yml` (see
`config/budgets.example.yml`) or set `COREAI_BUDGET_FILE`.

## Behaviour

- **Hard limit hit:** the request gets `429` with a JSON body
  `{"error": "budget_exceeded", "limit": "cost_usd|requests|tokens", "used": ..., "cap": ...}`
  and a `Retry-After` header with the seconds until the window resets.
- **Soft limit** (`warn_cost_usd`): never blocks. `GET /v1/usage` reports
  `"warning": true` once spend reaches it.
- **Windows** are fixed and aligned to UTC (a `day` window resets at 00:00 UTC).
- `0` blocks everything for that field; leaving a field out means no limit.
- An invalid file stops startup with every problem listed, so a typo never
  silently removes a limit.
- Failed calls that delivered nothing are not billed.
- **Streams are metered.** A stream counts as one request when it starts; its
  cost and tokens are added when it ends, including when it fails midway or the
  client disconnects, because the provider billed for the output already
  generated. Token counts come from the provider's SDK when it reports them;
  otherwise they are estimated (about 4 characters per token) and counted in
  `caips_stream_usage_estimated_total`.

## Per-model caps

A top-level `models:` section caps a model across all subjects combined, with
the same fields and windows as a subject (see `config/budgets.example.yml`).
It does not inherit `default`. A request is refused with the same `429` body
when the model it names is at its cap; the call's model is recorded afterwards
either way. Streams count toward the model when they end.

## Known limits

- Limits are checked before a call and usage recorded after (for a stream,
  after it ends), so one request
  can finish over the cap and concurrent requests can overshoot by what is in
  flight. The next request is refused.
- Usage is in memory, per process, and resets on restart. Subjects tracked are
  capped at 10,000 (least recently used dropped).
- Per-team budgets are not implemented (tokens carry no team claim yet).
- Model caps are checked before a call only when the request names the model;
  a request that relies on a provider's default model is counted afterwards but
  not blocked up front.

`GET /v1/usage` (scope `completions:read`) shows the caller's usage and limits.

## Usage reports

`GET /v1/usage/report` (scope `completions:read`) returns usage over time from
an in-memory ledger, as JSON or CSV.

| Parameter | Values |
| --- | --- |
| `bucket` | `day` (default) or `hour` |
| `group_by` | comma list of `subject`, `provider`, `model` |
| `since`, `until` | Unix times; `since` inclusive, `until` exclusive |
| `format` | `json` (default) or `csv` |

Callers see only their own usage; a token with the `admin` scope sees every
subject. Streams are recorded when they end. The ledger is bounded (30 days
and 50,000 rows) and resets on restart, so use the Prometheus metrics for
long-term history. CSV text cells starting with `=`, `+`, `-` or `@` are
prefixed with `'` so spreadsheets do not run them as formulas.

