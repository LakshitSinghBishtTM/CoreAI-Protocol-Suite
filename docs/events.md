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

## Slack and email

Both default to **alerts only**: `budget.*`, `provider.*` and runs that ended `failed`. Set an `*_EVENTS` list to choose exactly which events you get.

```
CAIPS_SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
CAIPS_SLACK_EVENTS=run.finished,budget.exceeded            # optional

CAIPS_SMTP_HOST=smtp.example.com
CAIPS_SMTP_FROM=caips@example.com
CAIPS_ALERT_EMAIL_TO=ops@example.com,oncall@example.com
CAIPS_SMTP_PORT=587  CAIPS_SMTP_USER=...  CAIPS_SMTP_PASSWORD=...
CAIPS_SMTP_STARTTLS=1                                      # set 0 only for a trusted local relay
CAIPS_EMAIL_EVENTS=budget.exceeded,provider.outage         # optional
```

Messages carry ids, names and amounts only: never prompts, tool arguments or keys. Email is
sent with STARTTLS by default. Like the webhook, delivery is best effort with no retries.
