# `caips-tui`

An interactive terminal UI over the HTTP API. Like the [`caips` CLI](cli.md), every button makes one documented request, so the UI cannot drift from the API and keeps no state of its own.

```bash
pip install -e '.[tui]'                 # adds Textual
export CAIPS_URL=http://localhost:6969  # default
caips-tui                               # or: caips tui [--section harness|chat|keys|runs|...]
```

## Getting started

First run: `caips tui` opens on **Home**, a three-line checklist (gateway reachable, token saved, provider key loaded). Paste the gateway token right there, press **Start chatting**, and you are in. Once a token is saved the TUI opens straight on **Chat**. Nothing is asked at startup and no dialog interrupts you.

## Layout

The left sidebar groups everything into modes and sections; the right side shows the one you picked.

| Group | Sections |
| --- | --- |
| **Start** | **Home**, **Chat**, **Keys**, **Settings** |
| Gateway | Providers, Routing, Models, Usage, Budgets, Status |
| Automation | Runs, Approvals, Tools, Agents, Tasks |
| Data | Memory, RAG, Feedback |
| Advanced | API playground, Local models, Fine-tuning, Evals, Admin |

The bottom of the sidebar shows whether the gateway answers `/health` and whether a token is set.

## Settings

Defaults for every new chat live in **Settings**, not in the chat screen: model, provider, routing policy, temperature, max tokens, history turns, reasoning, server tools, streaming, system prompt, and the theme. **Save** writes them to `~/.config/caips/tui.json` (mode follows your umask; no secrets are stored there) and Chat picks them up the next time it opens. **Reset to defaults** restores the factory values in the form, and **Sign out** removes the saved token. `Ctrl+T` toggles the theme and remembers it.

## Keys: the only place secrets are entered

Nothing is asked when the TUI starts. Open **Keys** (sidebar, or `Ctrl+K` from anywhere; Home can also save the token) and:

1. **Gateway token** at the top: paste it, press `Enter` or **Save token**. It is saved per gateway URL in `~/.config/caips/credentials.json` (mode `0600`; `$CAIPS_CONFIG_DIR` overrides the directory) and the `caips` CLI uses the same file. **Forget** removes it. The field is hidden and cleared after saving.
2. **Provider keys** below it (OpenAI, Claude, DeepSeek, ...): add them to the gateway; see the Keys row in the table below.

Lookup order for the gateway token: `--api-key-file PATH`, then `CAIPS_API_KEY` (for CI and containers), then the saved token. The token is never accepted as a command-line flag (other users could read it from the process list). `caips login` and `caips logout` manage the saved token from the shell.

## Chat

A chat session. Replies are rendered as Markdown (code blocks, lists, bold). It makes the same OpenAI-chat call as the API playground. The **Session settings** panel (collapsed by default) overrides the Settings defaults for the current conversation only.

| Control | What it does |
| --- | --- |
| Model, Provider, Routing policy | Pin them, or leave blank and the router picks. |
| Temperature, Max tokens | Sampling limits. |
| History turns | Send only the last N exchanges with each request (blank = all). The full history stays on screen. |
| System prompt | Free text, sent as the system message. |
| Context files | Path + `Enter` (or `/attach PATH`). Up to 5 files, 200,000 bytes each, sent in the system prompt on every turn. `/clear` keeps them; **Clear context** or `/detach` drops them. |
| **Incognito** | Sends `metadata.private = true`, so the gateway routes to local servers only (see [local-models](local-models.md); with no local server the request fails rather than going to a cloud provider). Feedback (Good/Bad, `/good`, `/bad`) and saving (`/save`) are switched off for the session. |
| **Tools** (below the transcript) | Sets `run_tools`: the gateway runs the tools your token is granted and continues the conversation. |
| **Reasoning** (below the transcript) | `off`, `brief` or `deep`: adds a "think step by step" instruction to the system prompt. The gateway has no reasoning parameter, so this is prompt-level and works with any model. |
| Stream | Streaming on or off. |

The settings panel collapses (click its title); it starts collapsed on terminals shorter than 40 rows. A line under the title shows the session state and a rough context size (characters ÷ 4, not a tokenizer count). `/help` lists the slash commands: `/attach /detach /incognito /reasoning /tools /stream /system /model /provider /policy /explain /retry /clear /good /bad /save`.

## Keys (shortcuts)

| Key | Action |
| --- | --- |
| `Ctrl+K` | open the Keys section with the token field focused |
| `Ctrl+B` | focus the section list (`↑`/`↓` switch section, `Enter` moves into it) |
| `Ctrl+P` | command palette: jump to any section or action |
| `Ctrl+T` | toggle light/dark theme |
| `Tab` / `Shift+Tab` | next / previous control |
| `F5`, `Ctrl+R` | refresh the section |
| `Esc` | stop a reply |
| `F1` | help |
| `Ctrl+Q` | quit |

## Sections

| Section | API | Notes |
| --- | --- | --- |
| Chat | `/v1/chat/completions` | See [Chat](#chat) above. |
| API (`--section chat`) | `/v1/chat/completions`, `/v1/completions[/stream]`, `/v1/messages`, `/v1/routing/explain`, `/v1/feedback` | Pick the dialect in the dropdown. Model, provider, policy, temperature, max tokens, system prompt; `stream` and `server tools` (`run_tools`) toggles. Slash commands: `/help /clear /retry /explain /good /bad /save /system /model /provider /policy /mode /stream`. A failed or stopped turn is dropped from history, so `/retry` is safe. |
| Providers | `/v1/providers` | Circuit state, health, p50 latency; refreshes every 5 s. |
| Routing | `/v1/admin/policies`, `/v1/routing/explain` | List policies (runtime-edited ones are marked). **Copy** loads a row into the fields, **Set** applies them as one policy at runtime, **Delete** removes one until restart (both `admin`, in memory only — edit `config/routing.yml` to keep a change; a `422` lists every problem in the detail view). **Explain** dry-runs a request under the selected policy: ranking, scores and exclusions, nothing sent. |
| Keys | `/v1/keys`, `/v1/keys/catalog` | The gateway token card sits above this. Add provider API keys without restarting the gateway (`admin`). Type a provider id (**Catalog** lists US labs, Chinese labs, `local` and `custom`), fill in the hidden key field and press **Add key**. Add more than one key per provider if you have them. `local` and `custom` take a name (label), base URL and model. The key field is cleared after sending, and the table shows only the last 4 characters. Environment-set keys appear read-only. |
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

## Web UI

A browser UI served by the gateway itself, in the style of a chat app: sidebar with chat history, one message box, and **Settings on its own page** (General, Provider keys, Chat defaults, Connection). Like the TUI it keeps no server state; every action is one documented API request.

```bash
caips web          # opens http://localhost:6969/app already signed in
```

`caips web` signs in with a saved token or, when `SECRET_KEY` is set, a 12 hour admin token. The token travels in the URL fragment, which browsers never send to a server; the page stores it in the browser and clears the address bar. Without either, `/app` shows a single "paste your token" screen.

| Area | What it does |
| --- | --- |
| Chat | Streaming chat, model picker, **Tools** (`run_tools`) and **Incognito** (local models only) toggles under the message box, Stop button. Chats are stored in the browser only; right-click a chat to delete it. |
| Agents | Agents from `config/agents.yml` (`GET /v1/harness/agents`), start a run with a task, recent runs with their trace events, cancel, and pending tool approvals. |
| Settings | Provider keys (add/remove), system prompt, temperature, max tokens, routing policy, history turns, theme, sign out. |
