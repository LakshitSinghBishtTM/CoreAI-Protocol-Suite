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

## Known limits

- Limits are checked before a call and usage recorded after (for a stream,
  after it ends), so one request
  can finish over the cap and concurrent requests can overshoot by what is in
  flight. The next request is refused.
- Usage is in memory, per process, and resets on restart. Subjects tracked are
  capped at 10,000 (least recently used dropped).
- Per team and per model budgets are not implemented yet.

`GET /v1/usage` (scope `completions:read`) shows the caller's usage and limits.
