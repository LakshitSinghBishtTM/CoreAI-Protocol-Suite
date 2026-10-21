# `caips` CLI

A thin command line over the HTTP API: each command is one documented request, so the CLI and the API cannot drift apart.

```bash
export CAIPS_URL=http://localhost:8000      # default
export CAIPS_API_KEY=...                    # bearer token
caips health
caips providers | jq '.[].name'
caips local pull TheBloke/some-model-GGUF:Q4_K_M    # admin
caips runs start calc "What is 17 * 23?"
caips approvals list
caips finetune estimate data.jsonl --base-model gpt-4o-mini
caips finetune submit data.jsonl --base-model gpt-4o-mini --max-cost 5
caips eval evals/ --model fast --baseline last.json
```

Output is JSON on stdout. Errors go to stderr. Exit codes: `0` ok, `1` HTTP or network error, `2` bad usage or input file.
`--url` overrides `CAIPS_URL`. Ids are percent-encoded before they go into a URL path.

Not yet covered: streaming run events (`runs events` returns the stored list), keys and budgets, routing policies, and a table output mode.
