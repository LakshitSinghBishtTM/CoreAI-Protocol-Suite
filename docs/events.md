# Events and webhooks

CaiPS emits events you can send to a signed webhook.

| Event | When | Fields |
|---|---|---|
| `run.finished` | an agent run ends (completed, failed, cancelled) | `run_id`, `agent`, `owner`, `status`, `reason`, `cost_usd` |
| `budget.warning` | a key's spend reaches `warn_cost_usd` (once per window) | `subject`, `used_usd`, `cap_usd` |
| `budget.exceeded` | spend reaches `max_cost_usd` (once per window) | `subject`, `used_usd`, `cap_usd` |
| `provider.outage` | a provider's circuit breaker opens | `provider` |
| `provider.recovered` | a half-open probe succeeds and the breaker closes | `provider` |

Every event also carries `id`, `type` and `ts`.

## Configure

```
CAIPS_EVENT_WEBHOOK_URL=https://example.com/hook
CAIPS_EVENT_WEBHOOK_SECRET=change-me
CAIPS_EVENT_WEBHOOK_EVENTS=run.finished,budget.exceeded   # optional filter
```

Requests are signed like the approval webhook: `X-CaiPS-Signature` is the hex HMAC-SHA256 of
`"<X-CaiPS-Timestamp>.<body>"`. Reject old timestamps on your side.

Delivery is fire and forget (5 s timeout, no retries); a failing receiver never slows requests.
Events are not stored, so use a durable queue behind your receiver if you cannot lose any.
