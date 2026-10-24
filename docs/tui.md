# `caips-tui`

An interactive terminal UI over the HTTP API. Like the [`caips` CLI](cli.md), every button makes one documented request, so the UI cannot drift from the API and keeps no state of its own.

```bash
pip install -e '.[tui]'                 # adds Textual
export CAIPS_URL=http://localhost:6969  # default
caips-tui                               # or: caips tui [--section runs]
```

## API token

You do not export a token to use the TUI. On first start, with no token found, it asks for one in a hidden prompt (`Esc` skips). Press `Ctrl+K` any time to set or replace it. The token is saved per gateway URL in `~/.config/caips/credentials.json` (mode `0600`; `$CAIPS_CONFIG_DIR` overrides the directory), and the `caips` CLI uses the same file. `caips login` and `caips logout` manage it from the shell.

Lookup order: `--api-key-file PATH`, then `CAIPS_API_KEY` (for CI and containers), then the saved token. The token is never accepted as a command-line flag (other users could read it from the process list). The sidebar shows whether a token is set and whether the gateway answers `/health`.

This is the *gateway* token. Provider keys (OpenAI, Claude, DeepSeek, ...) are separate and live in the **Keys** section below.

## Keys

| Key | Action |
| --- | --- |
| `Ctrl+K` | set or replace the API token |
| `Ctrl+B` | focus the section list (`↑`/`↓` switch section, `Enter` moves into it) |
| `Tab` / `Shift+Tab` | next / previous control |
| `F5`, `Ctrl+R` | refresh the section |
| `Esc` | stop a chat reply |
| `F1` | help |
| `Ctrl+Q` | quit |

## Sections

| Section | API | Notes |
| --- | --- | --- |
| Chat | `/v1/chat/completions`, `/v1/completions[/stream]`, `/v1/messages`, `/v1/routing/explain`, `/v1/feedback` | Pick the dialect in the dropdown. Model, provider, policy, temperature, max tokens, system prompt; `stream` and `server tools` (`run_tools`) toggles. Slash commands: `/help /clear /retry /explain /good /bad /save /system /model /provider /policy /mode /stream`. A failed or stopped turn is dropped from history, so `/retry` is safe. |
| Providers | `/v1/providers` | Circuit state, health, p50 latency; refreshes every 5 s. |
| Routing | `/v1/admin/policies`, `/v1/routing/explain` | List policies (runtime-edited ones are marked). **Copy** loads a row into the fields, **Set** applies them as one policy at runtime, **Delete** removes one until restart (both `admin`, in memory only — edit `config/routing.yml` to keep a change; a `422` lists every problem in the detail view). **Explain** dry-runs a request under the selected policy: ranking, scores and exclusions, nothing sent. |
| Keys | `/v1/keys`, `/v1/keys/catalog` | Add provider API keys without restarting the gateway (`admin`). Type a provider id (**Catalog** lists US labs, Chinese labs, `local` and `custom`), fill in the hidden key field and press **Add key**. Add more than one key per provider if you have them. `local` and `custom` take a name (label), base URL and model. The key field is cleared after sending, and the table shows only the last 4 characters. Environment-set keys appear read-only. |
| Models | `/v1/models/details`, `/v1/models` | Filter by provider, modality, minimum context. |
| Usage | `/v1/usage`, `/v1/usage/report` | Bucket and group-by; export CSV. |
| Budgets | `/v1/admin/budgets…` | One subject's window (limits, spent, resets in) in the table. **Set** applies the fields at runtime and **Remove** drops the override (`admin`, like `caips budget-set` / `budget-rm`). |
| Status | `/readyz`, `/health`, `/v1/stats`, `/v1/admin/shutdown`, `/metrics` | Stats and kill-switch status need `admin`. |
| Runs | `/v1/runs…` | Start, status, events, live **Follow** (SSE), pause, resume, approve/deny, cancel, kill all. |
| Approvals | `/v1/tools/approvals` | Approve or deny pending tool calls (`admin`). |
| Tools | `/v1/tools`, `/mcp` | **Call** runs a tool through MCP with JSON arguments. |
| Agents | `/v1/agents` | Register and terminate (experimental). |
| Tasks | `/v1/tasks` | Submit, look up, cancel. The API has no list endpoint, so the table shows ids seen in this session. |
| Memory | `/v1/memory…` | Save, search, delete, forget a namespace. |
| RAG | `/v1/rag/{collection}/…` | Ingest a local file (up to 2,000,000 characters), search, delete a source. |
| Feedback | `/v1/feedback…` | Submit, filter by rating, delete, export corrections as eval tasks (JSONL). |
| Local models | `/v1/local/models…` | Pull, inspect, unload, delete (`admin`). Pull waits without a timeout. |
| Fine-tuning | `/v1/finetune…` | Estimate before submit; submit asks first and honours a max cost. |
| Evals | `python -m evals` | Runs a suite through the gateway and streams output. The key goes to the child via its environment, not its arguments. |
| Admin | `/v1/admin/audit`, `/v1/admin/shutdown`, `/v1/runs/kill` | Audit log with filters; kill runs; emergency shutdown. |

Sections that need a scope you lack show the server's error in the status line. Kill, shutdown, delete, terminate, cancel and fine-tune submit ask for confirmation.

## Limits

- Routing and budget edits from the TUI are runtime overrides: they reset on restart (write `config/routing.yml` or `config/budgets.yml` to persist).
- Chat shows plain text (no Markdown rendering) and does not send images or client-side tool definitions.
- Verified against a fake gateway in `tests/test_caips_tui.py`, not against a live provider.
