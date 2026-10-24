# Agent harness: control and automation features

Configured in `config/agents.yml` (see `config/agents.example.yml`). Run API: `/v1/runs`.

## Human review requested by the agent

Set `ask_when_uncertain: true` on an agent. The model is offered a `request_human_review`
tool (`question`, optional `proposed_action`). Calling it pauses the run in
`waiting_approval`; an admin answers through the normal approval API. Approved or declined,
the decision is returned to the model as the tool result, and no real tool runs.

## Replay a run against a model

```python
from harness import replay_run
report = await replay_run(runner, run_id, model="some-model")
```

For each assistant turn in the stored history the model is asked again with the recorded
prefix. Recorded tool results are reused and **no tool is executed**, so replay is safe for
runs that used destructive tools. The report lists per turn whether tool names
(`same_tools`) and arguments (`same_arguments`) match, plus total cost. Turns before a
context compaction are gone and cannot be replayed.

## Sub-agents

`harness.subagent_tool(runner, "child")` returns a tool; register it and list it in the
parent's `allowed_tools`. The child is a normal run: own limits, own trace, only its own
tools. Nesting is capped at 2. If the child needs approval, the parent is told the child's
run id and the child stays paused for a human.

## Triggers

```yaml
triggers:
  nightly-report: {every_s: 86400, agent: reporter, task: "Summarize usage.", max_per_hour: 1}
  outage-triage:
    event: provider.outage
    match: {provider: openai}
    agent: reporter
    task: "Provider $provider is down; summarize impact."
```

Exactly one of `every_s` or `event`. A trigger never overlaps itself and is capped by
`max_per_hour` (default 6); skipped firings are counted. Event fields are inserted into the
task as short plain text. Not supported yet: cron expressions, inbound webhooks.

## Fixed-step workflows

`harness.run_workflow(parse_steps(steps), registry, route, input=...)` runs tool and model
steps in a fixed order; no model chooses the next step. `${input}` and `${step_id}` are
substituted. Tool output reaches a model prompt only as labeled untrusted data. Tool steps
obey allow lists, schemas, rate limits and approvals; a step needing approval stops the
workflow with `status="needs_approval"`, and a later call names it in `approved={"step_id"}`.
`max_cost_usd` stops before the next step once spent. Define them under `workflows:` in
`agents.yml` (`steps`, optional `max_cost_usd`). Endpoints: `GET /v1/workflows`,
`POST /v1/workflows/{name}/run` with `{"input": "...", "approved": ["step_id"]}` (scopes
`runs:read` / `runs:write`). Tools used must be granted to the caller; naming steps in
`approved` needs the `admin` scope. Runs are synchronous and model calls use the caller's
gateway budget. CLI: `caips workflows list`, `caips workflows run NAME INPUT [--approve ID]`.
