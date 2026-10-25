# Evals

Replay tasks against the gateway and score them. Cost and latency are recorded next to quality.

```bash
python -m evals evals/ --base-url http://localhost:6969 --model fast --out run.json
python -m evals evals/ --model fast --baseline run.json   # exit 1 if any task regressed
```

Set `CAIPS_API_KEY` if the gateway needs auth. Cost comes from the `X-CaiPS-Cost-USD` response header (non-streaming).

## Task files

```yaml
tasks:
  - id: capital
    prompt: "Capital of France, one word."
    scorer: {type: exact, expected: "Paris"}
    tags: [smoke]
```

Scorers: `exact`, `contains` (string or list, partial credit), `regex`, `json` (optionally match keys), `rubric` (model-graded, 0 to 10; needs a grader, see below).

## Rubric grading from the CLI

`--grader-model MODEL` (also on `caips eval`) sends each rubric prompt to that model through the same gateway and turns its 0 to 10 reply into a score. Without it, rubric tasks are recorded as errors that say a grader is needed. Grader calls are not included in the reported cost. Use a different model from the one under test.

## Agent tasks

Give a task `agent: <name>` and the suite starts that agent through `POST /v1/runs`, polls `GET /v1/runs/{id}` until it ends, and scores the run's `result` with the task's scorer. Cost, tokens and latency come from the run, and each result records `run_id`, `agent` and `steps`. A run that fails, is cancelled or stops on a limit scores 0 and the error says why. A run waiting for approval also counts as an error, so use agents that need no human in eval suites. The agent must be defined in `config/agents.yml` and the eval key needs the `runs:write` and `runs:read` scopes. `prompt` is the agent's task.

```yaml
tasks:
  - id: sum-with-tool
    agent: helper
    prompt: "What is 17 + 25?"
    scorer: {type: contains, expected: "42"}
```

To re-ask a model at each step of a past run instead, use `replay_run` (see `docs/harness.md`).

## From Python

`run_suite(tasks, route)` and `compare(tasks, {"cloud": route_a, "local": route_b})` take any async `route(CompletionRequest)`, so routing policies can be compared on the same tasks. `regressions(base, new)` lists tasks whose score dropped.
