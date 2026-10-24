# Routing policies

Policies live in the `policies:` section of `config/routing.yml`. The file is
validated as a whole and hot-reloaded; an invalid edit is rejected and the
previous policies stay active. A request picks a policy by name (`policy`
field); `default` is used otherwise.

## Fields

| Field | Meaning |
| --- | --- |
| `strategy` | `cost_aware`/`balanced`, `cheapest`, `latency_first`/`fastest`, `round_robin`, `fallback`, `explicit` |
| `weights.cost`, `weights.latency` | Relative weights for `cost_aware`. Normalised to sum to 1. Default 0.7 / 0.3 |
| `latency_ceiling_ms` | Providers whose median latency is above this are tried after those within it. They are not removed |
| `hedge_after_ms` | Start the next provider if no answer by then; first success wins |
| `fallback_chain` | Order to try providers (`fallback`), or the providers tried after the pinned one (`explicit`) |
| `provider`, `model` | The pinned provider and model for `explicit` |
| `fallback_model` | Model to use when failing over to another provider |
| `allow`, `deny` | Provider allow and deny lists. `deny` wins |

## How `cost_aware` scores a provider (lower wins)

```
score = cost_weight    * (estimated_cost / highest_estimated_cost)
      + latency_weight * (median_latency  / highest_median_latency)
      + health_weight  * (1 - health)
```

- Cost is estimated from the request's size and the price table
  (`config/pricing.yml`). Latency is the median of recent calls (500 ms when
  there is no history yet).
- `health` is 1.0 for a provider with no recent errors and falls with recent
  failures and slow calls (see `docs/operations.md`). `health_weight`
  defaults to 0.5, and `0` turns health scoring off.
- Providers whose circuit breaker is open are excluded before scoring.

## Worked examples

Two providers, `A` ($0.010, 400 ms) and `B` ($0.002, 1200 ms), both healthy.

| Policy | A | B | Picks |
| --- | --- | --- | --- |
| cost 0.7 / latency 0.3 | 0.7×1.0 + 0.3×0.33 = 0.80 | 0.7×0.2 + 0.3×1.0 = 0.44 | **B** |
| cost 0.1 / latency 0.9 | 0.1×1.0 + 0.9×0.33 = 0.40 | 0.1×0.2 + 0.9×1.0 = 0.92 | **A** |
| `cheapest` | | | **B** |
| `latency_first` | | | **A** |

If `B` has recent errors and a health of 0.4, its score gains
0.5×(1−0.4) = 0.30: 0.74 under the first policy, so `B` still wins there. At
health 0.0 the penalty is 0.50 and `A` (0.80) vs `B` (0.94) flips to **A**.

## Debugging a decision

`POST /v1/routing/explain` takes the same body as a completion and returns the
ranked providers with the score parts, exclusions (circuit open, denied,
above the latency ceiling) and the fallback order, without calling any
provider.

## Editing policies at runtime

Policies can be changed while the gateway runs, without editing the file
(`admin` scope). Like budget overrides, edits live in memory and are gone
after a restart: the file stays the source of truth across restarts.

- `PUT /v1/admin/policies/{name}` with the same fields as the file applies one
  policy now. The body is validated with the same rules and every problem is
  returned at once (`422`, `{"problems": [...]}`); an invalid edit never
  touches the active policies.
- `DELETE /v1/admin/policies/{name}` removes one policy until restart
  (`404` if it is not active).
- `GET /v1/admin/policies` lists the active set and marks which names were
  changed at runtime (`runtime_edits.edited` / `.removed`).

A runtime edit overrides a file policy of the same name and survives later
file reloads (including bad ones). Every change is audited (`policy.set`,
`policy.remove`) and visible in the TUI's Routing section, which also offers
a dry-run Explain against the selected policy.

```bash
caips policy-set cheap --strategy cheapest --weights 0.9,0.1 --deny grok
caips policy-set fast --file my-policy.yml     # whole body from YAML/JSON
caips policy-rm fast
```

## Related

- Budgets (what a key may spend): `docs/budgets.md`
- Failure behaviour, retries and breakers: `docs/operations.md`
