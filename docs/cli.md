# `caips` CLI

A thin command line over the HTTP API: each command is one documented request, so the CLI and the API cannot drift apart.

```bash
export CAIPS_URL=http://localhost:6969      # the default; set it only for another host or port
caips login                                 # prompts for the bearer token (hidden), saves it
caips health
caips providers | jq '.[].name'
caips local pull TheBloke/some-model-GGUF:Q4_K_M    # admin
caips runs start calc "What is 17 * 23?"
caips approvals list
caips explain "hello" --model fast          # dry run: routing decision, nothing sent
caips policies                              # admin, read-only
caips budgets --subject user:bob            # admin: limits and current usage
caips audit --limit 20 --action policy      # admin
caips finetune estimate data.jsonl --base-model gpt-4o-mini
caips finetune submit data.jsonl --base-model gpt-4o-mini --max-cost 5
caips eval evals/ --model fast --baseline last.json
```

The token is saved in `~/.config/caips/credentials.json` (mode `0600`), per `--url`; `caips logout` removes it. `CAIPS_API_KEY`, if set, overrides it (CI, containers). For scripts, `caips login < token-file` reads stdin.

Output is JSON on stdout. Errors go to stderr. Exit codes: `0` ok, `1` HTTP or network error, `2` bad usage or input file.
`--url` overrides `CAIPS_URL`. Ids are percent-encoded before they go into a URL path.

## Provider keys

```bash
caips keys catalog                       # provider ids by region
caips keys add openai                    # hidden prompt; or: echo "$KEY" | caips keys add openai
caips keys add openai --label work       # a second key for the same provider
caips keys add custom --label mylab --base-url https://llm.example.com/v1 --model m1
caips keys add local --label ollama      # preset URL; no key needed
caips keys list                          # masked: only the last 4 characters
caips keys rm KEY_ID
```

These need an `admin` token. The key is never accepted as a flag. See the README for how several keys per provider behave.

Not yet covered: budgets, routing policies, and a table output mode.

## Streaming

- `caips chat MODEL "prompt"` streams a chat completion, printing text as it arrives. A mid-stream error event exits 1.
- `caips runs watch RUN_ID` follows `/v1/runs/{id}/stream` and prints one JSON event per line until the run ends or blocks.

For an interactive view of the same API, run `caips tui` (or `caips-tui`); see [tui.md](tui.md).
