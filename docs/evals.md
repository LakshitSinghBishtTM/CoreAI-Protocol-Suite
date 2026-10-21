# Evals

Replay tasks against the gateway and score them. Cost and latency are recorded next to quality.

```bash
python -m evals evals/ --base-url http://localhost:8000 --model fast --out run.json
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

Scorers: `exact`, `contains` (string or list, partial credit), `regex`, `json` (optionally match keys), `rubric` (model-graded, 0 to 10; needs a grader, API use only for now).

## From Python

`run_suite(tasks, route)` and `compare(tasks, {"cloud": route_a, "local": route_b})` take any async `route(CompletionRequest)`, so routing policies can be compared on the same tasks. `regressions(base, new)` lists tasks whose score dropped.
