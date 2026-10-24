"""One pane per API area. Each action is a single documented request."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Grid
from textual.widgets import Button, Input, RichLog

from caips_tui.client import ApiError, q
from caips_tui.widgets import Action, Field, Pane, TablePane

MAX_RAG_CHARS = 2_000_000  # the server refuses more (api/rag_api.py)


def read_jsonl(path: str) -> list[dict]:
    """One JSON object per line; same rules and messages as ``caips finetune``."""
    from caips_cli.cli import CliError, _jsonl

    try:
        return _jsonl(path)
    except OSError as e:
        raise CliError(f"cannot read {path}: {e.strerror or e}")


class KeysPane(TablePane):
    TITLE = "Keys"
    HELP = (
        "Provider API keys (admin). Pick a provider id from Catalog, add as many keys per "
        "provider as you like (they rotate; an exhausted key is skipped), or add a Local "
        "or Custom server. Keys are encrypted on the server and never shown again."
    )
    PATH = "/v1/keys"
    ID_KEY = "id"
    COLUMNS = [
        ("Provider", "provider"),
        ("Label", "label"),
        ("Key", "key"),
        ("Source", "source"),
        ("Loaded", "loaded"),
        ("Endpoint", "base_url"),
        ("Model", "model"),
    ]
    FIELDS = [
        Field("provider", "provider id (openai, qwen, local, custom, ...)"),
        Field("label", "label / server name (optional for cloud)"),
        Field("key", "API key (hidden)", password=True),
        Field("base_url", "base URL (local/custom, optional override)"),
        Field("model", "default model (optional)"),
    ]
    ACTIONS = [
        Action("add", "Add key"),
        Action("catalog", "Catalog"),
        Action(
            "remove",
            "Remove",
            needs_row=True,
            confirm="Remove the {provider} key '{label}'?",
            variant="error",
        ),
    ]

    async def act_add(self, row: Optional[dict]) -> None:
        provider = self.val("provider")
        if not provider:
            self.say("Give a provider id (press Catalog to list them).", error=True)
            return
        body = {
            "provider": provider,
            "api_key": self.query_one("#fld-key", Input).value,
            "label": self.val("label"),
            "base_url": self.val("base_url"),
            "model": self.val("model"),
        }
        ok, data = await self.api("POST", "/v1/keys", json=body)
        if not ok:
            return
        self.set_val("key", "")  # never leave the secret in the widget
        note = (
            ""
            if data.get("loaded")
            else f" (not loaded: {data.get('problem') or 'see server log'})"
        )
        self.say(
            f"added {data.get('provider')}/{data.get('label')}{note}", error=bool(note)
        )
        await self.refresh_data()

    async def act_catalog(self, row: Optional[dict]) -> None:
        ok, data = await self.api("GET", "/v1/keys/catalog")
        if not ok:
            return
        groups: dict[str, list[str]] = {}
        names = {
            "us": "United States",
            "cn": "China",
            "self": "Self-hosted",
            "any": "Any OpenAI-compatible",
        }
        for lab in data.get("data", []):
            groups.setdefault(names.get(lab["region"], lab["region"]), []).append(
                f"{lab['id']:<11} {lab['label']}"
            )
        self.render_detail({k: v for k, v in groups.items()})
        self.say("provider ids are in the left column of the detail view")

    async def act_remove(self, row: dict) -> None:
        ok, _ = await self.api("DELETE", f"/v1/keys/{q(row['id'])}")
        if ok:
            self.say(f"removed {row.get('provider')}/{row.get('label')}")
            await self.refresh_data()


# ---------------------------------------------------------------- overview
class ProvidersPane(TablePane):
    TITLE = "Providers"
    HELP = "Circuit-breaker state, health score and latency per provider. Updates every 5 s."
    PATH = "/v1/providers"
    REFRESH = 5
    ID_KEY = "name"
    COLUMNS = [
        ("Provider", "name"),
        ("Up", "available"),
        ("Circuit", "circuit_state"),
        ("Health", "health"),
        ("p50 ms", "latency_ms"),
        ("Models", "n_models"),
        ("Errors", "error_kinds"),
    ]

    def unwrap(self, data: Any) -> list[dict]:
        rows = super().unwrap(data)
        for r in rows:
            r["n_models"] = len(r.get("model_list") or [])
        return rows


class ModelsPane(TablePane):
    TITLE = "Models"
    HELP = "Model registry (prices, context, capabilities). Filters apply on Refresh / Enter."
    PATH = "/v1/models/details"
    ID_KEY = "model_id"
    FIELDS = [
        Field("provider", "provider filter"),
        Field("modality", "modality filter (e.g. vision)"),
        Field("min_context", "min context window"),
    ]
    ACTIONS = [Action("gateway", "Model ids")]
    COLUMNS = [
        ("Model", "model_id"),
        ("Provider", "provider"),
        ("Tier", "tier"),
        ("Context", "context_window"),
        ("Max out", "max_output_tokens"),
        ("$/M in", "input_per_million_usd"),
        ("$/M out", "output_per_million_usd"),
        ("Tools", "supports_function_calling"),
        ("Modalities", "modalities"),
    ]

    def params(self) -> dict:
        return {
            "provider": self.val("provider"),
            "modality": self.val("modality"),
            "min_context": self.val("min_context"),
        }

    async def act_gateway(self, row: Optional[dict]) -> bool:
        """``GET /v1/models``: what the gateway will accept as ``model``, aliases included."""
        ok, data = await self.api("GET", "/v1/models")
        if ok:
            self.render_detail(
                [f"{m['id']}  ({m.get('owned_by', '')})" for m in data.get("data", [])]
            )
            self.say("OpenAI-compatible model list")
        return False


class RoutingPane(TablePane):
    TITLE = "Routing"
    HELP = (
        "Routing policies from config/routing.yml plus runtime edits (admin). Set "
        "applies the fields as one policy and Delete removes one, both in memory "
        "until restart: edit the file to keep a change. Copy loads a row into the "
        "fields; Explain dry-runs a request and shows the ranking (nothing is sent)."
    )
    PATH = "/v1/admin/policies"
    ID_KEY = "name"
    COLUMNS = [
        ("Policy", "name"),
        ("Strategy", "strategy"),
        ("Cost w", "cost_w"),
        ("Latency w", "latency_w"),
        ("Ceiling ms", "latency_ceiling_ms"),
        ("Hedge ms", "hedge_after_ms"),
        ("Chain", "chain"),
        ("Provider", "provider"),
        ("Runtime", "runtime"),
    ]
    FIELDS = [
        Field("name", "policy name (Set)"),
        Field(
            "strategy",
            "cost_aware|balanced|cheapest|fastest|round_robin|fallback|explicit",
        ),
        Field("weights", "cost,latency, e.g. 0.7,0.3"),
        Field("latency_ceiling_ms", "latency ceiling ms"),
        Field("hedge_after_ms", "hedge after ms"),
        Field("fallback_chain", "chain: openai,anthropic"),
        Field("provider", "pinned provider (explicit)"),
        Field("model", "model"),
        Field("fallback_model", "failover model"),
        Field("allow", "allow list: a,b"),
        Field("deny", "deny list: a,b"),
        Field("prompt", "prompt for Explain"),
    ]
    ACTIONS = [
        Action("set", "Set"),
        Action("copy", "Copy", needs_row=True),
        Action("explain", "Explain"),
        Action(
            "delete",
            "Delete",
            needs_row=True,
            confirm="Remove policy '{name}' until restart?",
            variant="error",
        ),
    ]

    def unwrap(self, data: Any) -> list[dict]:
        if not isinstance(data, dict):
            return []
        edited = set((data.get("runtime_edits") or {}).get("edited") or [])
        rows = []
        for r in data.get("policies") or []:
            r = dict(r)
            weights = r.pop("weights", None) or {}
            r["cost_w"] = weights.get("cost")
            r["latency_w"] = weights.get("latency")
            r["chain"] = ",".join(r.get("fallback_chain") or [])
            r["runtime"] = "edited" if r.get("name") in edited else ""
            rows.append(r)
        return rows

    def status_line(self, rows: list[dict]) -> str:
        edited = sum(1 for r in rows if r.get("runtime"))
        note = f", {edited} runtime edit(s)" if edited else ""
        return f"{len(rows)} policy(ies){note}"

    def _body_from_fields(self) -> Optional[dict]:
        """The policy body the Set fields describe, or None (and said) if invalid."""
        body: dict[str, Any] = {}
        if self.val("strategy"):
            body["strategy"] = self.val("strategy")
        raw_w = self.val("weights")
        if raw_w:
            parts = [p for p in raw_w.replace(" ", "").split(",") if p]
            try:
                if len(parts) != 2:
                    raise ValueError
                body["weights"] = {"cost": float(parts[0]), "latency": float(parts[1])}
            except ValueError:
                self.say("weights: two numbers, e.g. 0.7,0.3", error=True)
                return None
        for fid in ("latency_ceiling_ms", "hedge_after_ms"):
            raw = self.val(fid)
            if raw:
                try:
                    body[fid] = float(raw)
                except ValueError:
                    self.say(f"{fid}: needs a number", error=True)
                    return None
        for fid in ("fallback_chain", "allow", "deny"):
            raw = self.val(fid)
            if raw:
                body[fid] = [v.strip() for v in raw.split(",") if v.strip()]
        for fid in ("provider", "model", "fallback_model"):
            if self.val(fid):
                body[fid] = self.val(fid)
        return body

    async def act_set(self, row: Optional[dict]) -> bool:
        name = self.val("name")
        if not name:
            self.say("Give a policy name first.", error=True)
            return False
        body = self._body_from_fields()
        if body is None:
            return False
        ok, data = await self.api("PUT", f"/v1/admin/policies/{q(name)}", json=body)
        if not ok:
            problems = data.get("problems") if isinstance(data, dict) else None
            if problems:
                self.render_detail(problems)
                self.say(
                    f"rejected: {len(problems)} problem(s), see detail", error=True
                )
            return False
        self.say(f"policy '{name}' set (in memory until restart)")
        return True

    async def act_copy(self, row: dict) -> bool:
        """Load a row into the fields so it can be edited and Set again."""

        def fmt(v: Any) -> str:
            return "" if v is None else str(v)

        def num(v: Any) -> str:
            return fmt(round(v, 4)) if isinstance(v, float) else fmt(v)

        self.set_val("name", fmt(row.get("name")))
        self.set_val("strategy", fmt(row.get("strategy")))
        w = row.get("weights") or {}
        self.set_val(
            "weights", f"{num(w.get('cost'))},{num(w.get('latency'))}" if w else ""
        )
        self.set_val("latency_ceiling_ms", num(row.get("latency_ceiling_ms")))
        self.set_val("hedge_after_ms", num(row.get("hedge_after_ms")))
        self.set_val("fallback_chain", ",".join(row.get("fallback_chain") or []))
        self.set_val("provider", fmt(row.get("provider")))
        self.set_val("model", fmt(row.get("model")))
        self.set_val("fallback_model", fmt(row.get("fallback_model")))
        self.set_val("allow", ",".join(row.get("allow") or []))
        self.set_val("deny", ",".join(row.get("deny") or []))
        self.say(f"'{row.get('name')}' loaded into the fields; edit, then Set")
        return False

    async def act_explain(self, row: Optional[dict]) -> bool:
        """``POST /v1/routing/explain``: where a request would go, and why."""
        policy = (row or {}).get("name") or self.val("name")
        body: dict[str, Any] = {
            "messages": [{"role": "user", "content": self.val("prompt") or "Hello"}]
        }
        if policy:
            body["policy"] = policy
        ok, data = await self.api("POST", "/v1/routing/explain", json=body)
        if ok:
            self.render_detail(data)
            self.say(f"explain for policy '{policy or 'default'}' (no request sent)")
        return False

    async def act_delete(self, row: dict) -> bool:
        ok, _ = await self.api("DELETE", f"/v1/admin/policies/{q(row['name'])}")
        if ok:
            self.say(f"policy '{row['name']}' removed (until restart)")
        return bool(ok)


class UsagePane(TablePane):
    TITLE = "Usage"
    HELP = "Requests, tokens and cost over time (in-memory ledger; resets on restart). Admins see every subject."
    PATH = "/v1/usage/report"
    FIELDS = [
        Field("bucket", "hour or day", "day"),
        Field("group_by", "group by: subject,provider,model", "provider,model"),
        Field("hours", "last N hours (blank = all)"),
        Field("csv", "CSV path for Export", "usage.csv"),
    ]
    ACTIONS = [Action("budget", "Budget"), Action("export", "Export")]

    def params(self) -> dict:
        p: dict[str, Any] = {
            "bucket": self.val("bucket") or "day",
            "group_by": self.val("group_by"),
        }
        hours = self.val("hours")
        if hours:
            try:
                p["since"] = time.time() - float(hours) * 3600
            except ValueError:
                pass
        return p

    async def act_budget(self, row: Optional[dict]) -> bool:
        ok, data = await self.api("GET", "/v1/usage")
        if ok:
            self.render_detail(data)
            self.say(f"budget window for {data.get('subject', '?')}")
        return False

    async def act_export(self, row: Optional[dict]) -> bool:
        ok, text = await self.api(
            "GET", "/v1/usage/report", params={**self.params(), "format": "csv"}
        )
        if ok:
            path = Path(self.val("csv") or "usage.csv")
            await asyncio.to_thread(
                path.write_text,
                text if isinstance(text, str) else json.dumps(text),
                "utf-8",
            )
            self.say(f"wrote {path}")
        return False


class BudgetsPane(TablePane):
    TITLE = "Budgets"
    HELP = (
        "Spend caps per key subject (admin). The table shows one subject's window: "
        "type a subject (or leave blank for yourself) and press Enter. Set applies "
        "the fields at runtime (until restart, like `caips budget-set`); Remove "
        "drops the override so the default applies again."
    )
    PATH = "/v1/admin/budgets"
    ID_KEY = "subject"
    COLUMNS = [
        ("Subject", "subject"),
        ("Spent $", "spent_usd"),
        ("Max $", "max_cost_usd"),
        ("Requests", "requests"),
        ("Max req", "max_requests"),
        ("Tokens", "tokens"),
        ("Max tok", "max_tokens"),
        ("Resets s", "resets_in_s"),
        ("Warn", "warning"),
    ]
    FIELDS = [
        Field("subject", "key subject (blank: yourself)"),
        Field("max_cost_usd", "max spend USD"),
        Field("warn_cost_usd", "warn at USD"),
        Field("max_requests", "max requests"),
        Field("max_tokens", "max tokens"),
        Field("window_seconds", "window seconds", "86400"),
    ]
    ACTIONS = [
        Action("set", "Set"),
        Action(
            "rm",
            "Remove",
            needs_row=True,
            confirm="Remove the budget override for '{subject}'?",
            variant="error",
        ),
    ]

    def params(self) -> dict:
        return {"subject": self.val("subject")}

    def unwrap(self, data: Any) -> list[dict]:
        if not isinstance(data, dict) or not data.get("enabled"):
            return []
        st = data.get("status")
        if not st:
            return [{"subject": data.get("subject"), "warning": "no limits"}]
        used, lim = st.get("used") or {}, st.get("limits") or {}
        return [
            {
                "subject": data.get("subject"),
                "spent_usd": used.get("cost_usd"),
                "max_cost_usd": lim.get("cost_usd"),
                "requests": used.get("requests"),
                "max_requests": lim.get("requests"),
                "tokens": used.get("tokens"),
                "max_tokens": lim.get("tokens"),
                "resets_in_s": st.get("resets_in_seconds"),
                "warning": "yes" if st.get("warning") else "",
            }
        ]

    async def act_set(self, row: Optional[dict]) -> bool:
        subject = self.val("subject")
        if not subject:
            self.say(
                "Give a subject first (blank means yourself; type it out).", error=True
            )
            return False
        body: dict[str, Any] = {}
        for fid in ("max_cost_usd", "warn_cost_usd"):
            raw = self.val(fid)
            if raw:
                try:
                    body[fid] = float(raw)
                except ValueError:
                    self.say(f"{fid}: needs a number", error=True)
                    return False
        for fid in ("max_requests", "max_tokens"):
            raw = self.val(fid)
            if raw:
                try:
                    body[fid] = int(raw)
                except ValueError:
                    self.say(f"{fid}: needs an integer", error=True)
                    return False
        if not body:
            self.say("Give at least one limit (max or warn).", error=True)
            return False
        try:
            body["window_seconds"] = int(self.val("window_seconds") or 86400)
        except ValueError:
            self.say("window_seconds: needs an integer", error=True)
            return False
        ok, data = await self.api("PUT", f"/v1/admin/budgets/{q(subject)}", json=body)
        if ok:
            self.say(f"budget for '{subject}' set (in memory until restart)")
        return bool(ok)

    async def act_rm(self, row: dict) -> bool:
        ok, _ = await self.api("DELETE", f"/v1/admin/budgets/{q(row['subject'])}")
        if ok:
            self.say(f"override for '{row['subject']}' removed (default applies)")
        return bool(ok)


class StatusPane(Pane):
    TITLE = "Status"
    HELP = "Health, metrics and runtime statistics. Stats and kill-switch status need the admin scope."
    REFRESH = 10
    VIEWS = [
        ("ready", "Readiness", "/readyz"),
        ("health", "Health", "/health"),
        ("stats", "Stats", "/v1/stats"),
        ("kill", "Kill switch", "/v1/admin/shutdown"),
        ("metrics", "Metrics", "/metrics"),
    ]

    def __init__(self, *a: Any, **kw: Any):
        super().__init__(*a, **kw)
        self.current = "ready"

    def body(self) -> ComposeResult:
        with Grid(classes="actions"):
            for vid, label, _ in self.VIEWS:
                yield Button(
                    label,
                    id=f"view-{vid}",
                    variant="primary" if vid == self.current else "default",
                )
        yield RichLog(classes="detail wide", wrap=True, markup=False, highlight=False)

    async def refresh_data(self) -> None:
        path = next(p for v, _, p in self.VIEWS if v == self.current)
        ok, data = await self.api("GET", path)
        if ok:
            self.render_detail(data)
            self.say(f"GET {path}")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        bid = event.button.id or ""
        if bid.startswith("view-"):
            self.current = bid[5:]
            for b in self.query(Button):
                b.variant = "primary" if b.id == bid else "default"
            await self.refresh_data()


# ------------------------------------------------------------------ agents
class RunsPane(TablePane):
    TITLE = "Runs"
    HELP = "Agent harness runs. Approve/Deny answer a run that is waiting for approval (admin)."
    PATH = "/v1/runs"
    REFRESH = 3
    EXCLUSIVE = ("follow",)
    COLUMNS = [
        ("Run", "id"),
        ("Agent", "agent"),
        ("Status", "status"),
        ("Step", "step"),
        ("Cost $", "cost_usd"),
        ("Stop reason", "stop_reason"),
        ("Owner", "owner"),
    ]
    FIELDS = [
        Field("agent", "agent name"),
        Field("task", "task for the agent"),
        Field("owner", "owner for Kill (blank = everyone)"),
    ]
    ACTIONS = [
        Action("start", "Start"),
        Action("status", "Status", needs_row=True),
        Action("events", "Events", needs_row=True),
        Action("follow", "Follow", needs_row=True),
        Action("pause", "Pause", needs_row=True),
        Action("resume", "Resume", needs_row=True),
        Action("approve", "Approve", needs_row=True, variant="success"),
        Action("deny", "Deny", needs_row=True, variant="warning"),
        Action("cancel", "Cancel", needs_row=True, confirm="Cancel run {id}?"),
        Action(
            "kill",
            "Kill all",
            confirm="Kill switch: stop ALL active runs (or only the owner given)?",
            variant="error",
        ),
    ]

    async def act_start(self, row: Optional[dict]) -> None:
        agent, task = self.val("agent"), self.val("task")
        if not agent or not task:
            self.say("Fill in agent and task.", error=True)
            return
        ok, data = await self.api(
            "POST", "/v1/runs", json={"agent": agent, "task": task}
        )
        if ok:
            self.say(f"started run {data.get('id', '?')}")

    async def act_status(self, row: dict) -> bool:
        ok, data = await self.api("GET", f"/v1/runs/{q(row['id'])}")
        if ok:
            self.render_detail(data)
        return False

    async def act_events(self, row: dict) -> bool:
        ok, data = await self.api("GET", f"/v1/runs/{q(row['id'])}/events")
        if ok:
            log = self.detail_log()
            log.clear()
            events = data.get("data", [])
            for e in events:
                log.write(Text(json.dumps(e, default=str, ensure_ascii=False)))
            self.say(f"{len(events)} event(s)")
        return False

    async def act_follow(self, row: dict) -> bool:
        """Live trace over SSE until the run ends or blocks. Press Follow on another run to switch."""
        log = self.detail_log()
        log.clear()
        n = 0
        try:
            async for _, data in self.client.stream(
                "GET", f"/v1/runs/{q(row['id'])}/stream"
            ):
                if data == "[DONE]":
                    break
                n += 1
                log.write(Text(data))
        except ApiError as e:
            self.say(str(e), error=True)
            return False
        self.say(f"stream ended after {n} event(s)")
        return False

    async def _post(
        self, row: dict, verb: str, body: Optional[dict] = None, ok_msg: str = ""
    ) -> None:
        ok, data = await self.api("POST", f"/v1/runs/{q(row['id'])}/{verb}", json=body)
        if ok:
            self.say(ok_msg or f"{verb}: {json.dumps(data, default=str)}")

    async def act_pause(self, row: dict) -> None:
        await self._post(row, "pause")

    async def act_resume(self, row: dict) -> None:
        await self._post(row, "resume", {})

    async def act_approve(self, row: dict) -> None:
        await self._post(row, "resume", {"approve": True}, "approved")

    async def act_deny(self, row: dict) -> None:
        await self._post(row, "resume", {"approve": False}, "denied")

    async def act_cancel(self, row: dict) -> None:
        await self._post(row, "cancel")

    async def act_kill(self, row: Optional[dict]) -> None:
        ok, data = await self.api(
            "POST", "/v1/runs/kill", json={"owner": self.val("owner") or None}
        )
        if ok:
            self.say(f"killed {data.get('killed', 0)} run(s)")


class ApprovalsPane(TablePane):
    TITLE = "Tool approvals"
    HELP = "Tool calls waiting for a human decision (admin). Updates every 3 s."
    PATH = "/v1/tools/approvals"
    REFRESH = 3
    COLUMNS = [
        ("Id", "id"),
        ("Caller", "caller"),
        ("Tool", "tool"),
        ("Status", "status"),
        ("By", "decided_by"),
        ("Arguments", "arguments"),
    ]
    ACTIONS = [
        Action(
            "approve",
            "Approve",
            needs_row=True,
            variant="success",
            confirm="Approve {tool} for {caller}?",
        ),
        Action("deny", "Deny", needs_row=True, variant="warning"),
    ]

    async def _decide(self, row: dict, approve: bool) -> None:
        ok, data = await self.api(
            "POST", f"/v1/tools/approvals/{q(row['id'])}", json={"approve": approve}
        )
        if ok:
            self.say(f"{data.get('tool', '?')}: {data.get('status', '?')}")

    async def act_approve(self, row: dict) -> None:
        await self._decide(row, True)

    async def act_deny(self, row: dict) -> None:
        await self._decide(row, False)


class ToolsPane(TablePane):
    TITLE = "Tools"
    HELP = 'Tools you may use. Call runs one via MCP; arguments are a JSON object, e.g. {"expression": "17*23"}.'
    PATH = "/v1/tools"
    ID_KEY = "name"
    COLUMNS = [
        ("Tool", "name"),
        ("Version", "version"),
        ("Permission", "permission"),
        ("Owner", "owner"),
        ("Description", "description"),
    ]
    FIELDS = [Field("args", "arguments (JSON object)", "{}")]
    ACTIONS = [
        Action("call", "Call", needs_row=True, variant="success"),
        Action("mcp", "MCP list"),
    ]

    async def _mcp(self, method: str, params: dict) -> Any:
        ok, data = await self.api(
            "POST",
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )
        if not ok:
            return None
        if isinstance(data, dict) and "error" in data:
            self.say(f"MCP error: {json.dumps(data['error'], default=str)}", error=True)
            self.render_detail(data["error"])
            return None
        return data.get("result") if isinstance(data, dict) else data

    async def act_call(self, row: dict) -> bool:
        try:
            args = json.loads(self.val("args") or "{}")
            if not isinstance(args, dict):
                raise ValueError("must be an object")
        except ValueError as e:
            self.say(f"arguments: {e}", error=True)
            return False
        result = await self._mcp("tools/call", {"name": row["name"], "arguments": args})
        if result is not None:
            self.render_detail(result)
            self.say(f"called {row['name']}")
        return False

    async def act_mcp(self, row: Optional[dict]) -> bool:
        result = await self._mcp("tools/list", {})
        if result is not None:
            self.render_detail(result)
            self.say("MCP tools/list")
        return False


class AgentsPane(TablePane):
    TITLE = "Agents"
    HELP = "Experimental lifecycle-managed agents (no tools yet; see agents/README.md)."
    PATH = "/v1/agents"
    ID_KEY = "agent_id"
    COLUMNS = [
        ("Agent", "agent_id"),
        ("Status", "status"),
        ("Tasks", "task_count"),
        ("Errors", "error_count"),
        ("Last active", "last_active"),
        ("Created", "created_at"),
    ]
    FIELDS = [Field("name", "name"), Field("caps", "capabilities (comma separated)")]
    ACTIONS = [
        Action("register", "Register"),
        Action(
            "terminate",
            "Terminate",
            needs_row=True,
            confirm="Terminate agent {agent_id}?",
            variant="error",
        ),
    ]

    async def act_register(self, row: Optional[dict]) -> None:
        name = self.val("name")
        if not name:
            self.say("Give the agent a name.", error=True)
            return
        caps = [c.strip() for c in self.val("caps").split(",") if c.strip()]
        ok, data = await self.api(
            "POST", "/v1/agents", json={"name": name, "capabilities": caps}
        )
        if ok:
            self.say(f"registered {data.get('agent_id', '?')}")

    async def act_terminate(self, row: dict) -> None:
        ok, _ = await self.api("DELETE", f"/v1/agents/{q(row['agent_id'])}")
        if ok:
            self.say(f"terminated {row['agent_id']}")


class TasksPane(TablePane):
    """Background tasks. The API has no list endpoint, so this pane follows the ids it has seen."""

    TITLE = "Tasks"
    HELP = "Submit a task to an agent, or look one up by id. The list shows tasks seen in this session."
    ID_KEY = "task_id"
    REFRESH = 3
    COLUMNS = [
        ("Task", "task_id"),
        ("Agent", "agent_id"),
        ("Status", "status"),
        ("Iter", "iterations"),
        ("Objective", "objective"),
        ("Error", "error"),
    ]
    FIELDS = [
        Field("agent", "agent id"),
        Field("objective", "objective"),
        Field("max_iter", "max iterations", "10"),
        Field("priority", "priority 0-3", "2"),
        Field("task", "task id to look up"),
    ]
    ACTIONS = [
        Action("submit", "Submit"),
        Action("lookup", "Look up"),
        Action(
            "cancel",
            "Cancel",
            needs_row=True,
            confirm="Cancel task {task_id}?",
            variant="warning",
        ),
    ]

    def __init__(self, *a: Any, **kw: Any):
        super().__init__(*a, **kw)
        self.known: list[str] = []

    async def fetch(self) -> list[dict]:
        rows = []
        for tid in list(self.known):
            try:
                rows.append(await self.client.request("GET", f"/v1/tasks/{q(tid)}"))
            except ApiError as e:
                if e.status == 404:
                    self.known.remove(tid)
                else:
                    self.say(str(e), error=True)
        return rows

    async def act_submit(self, row: Optional[dict]) -> None:
        agent, objective = self.val("agent"), self.val("objective")
        if not agent or not objective:
            self.say("Fill in agent id and objective.", error=True)
            return
        try:
            body = {
                "agent_id": agent,
                "objective": objective,
                "max_iterations": int(self.val("max_iter") or 10),
                "priority": int(self.val("priority") or 2),
            }
        except ValueError:
            self.say("max iterations and priority must be whole numbers.", error=True)
            return
        ok, data = await self.api("POST", "/v1/tasks", json=body)
        if ok:
            tid = data.get("task_id")
            if tid and tid not in self.known:
                self.known.append(tid)
            self.say(f"submitted {tid}")

    async def act_lookup(self, row: Optional[dict]) -> None:
        tid = self.val("task")
        if not tid:
            self.say("Enter a task id.", error=True)
            return
        ok, _ = await self.api("GET", f"/v1/tasks/{q(tid)}")
        if ok:
            if tid not in self.known:
                self.known.append(tid)
            self.say(f"tracking {tid}")

    async def act_cancel(self, row: dict) -> None:
        ok, _ = await self.api("DELETE", f"/v1/tasks/{q(row['task_id'])}")
        if ok:
            self.say(f"cancelled {row['task_id']}")


# -------------------------------------------------------------- data stores
class MemoryPane(TablePane):
    TITLE = "Memory"
    HELP = "Key/value memory per caller and namespace. Search matches words in keys and values."
    PATH = "/v1/memory"
    COLUMNS = [
        ("Namespace", "ns"),
        ("Key", "key"),
        ("Value", "value"),
        ("Updated", "updated"),
    ]
    FIELDS = [
        Field("ns", "namespace (blank = all; 'default' when writing)"),
        Field("key", "key"),
        Field("value", "value"),
        Field("query", "search words"),
    ]
    ACTIONS = [
        Action("put", "Save"),
        Action("search", "Search"),
        Action(
            "delete",
            "Delete",
            needs_row=True,
            confirm="Forget {ns}/{key}?",
            variant="warning",
        ),
        Action(
            "forget",
            "Forget ns",
            confirm="Forget EVERYTHING in the namespace field (everything you stored, if blank)?",
            variant="error",
        ),
    ]

    def row_id(self, row: dict, i: int) -> str:
        return f"{row.get('ns', '')}/{row.get('key', i)}"

    def params(self) -> dict:
        return {"ns": self.val("ns")}

    async def act_put(self, row: Optional[dict]) -> None:
        key = self.val("key") or (row or {}).get("key", "")
        value = self.val("value")
        if not key or not value:
            self.say("Fill in key and value.", error=True)
            return
        ns = self.val("ns") or "default"
        ok, _ = await self.api(
            "PUT", f"/v1/memory/{q(key)}", params={"ns": ns}, json={"value": value}
        )
        if ok:
            self.say(f"saved {ns}/{key}")

    async def act_search(self, row: Optional[dict]) -> bool:
        text = self.val("query")
        if not text:
            self.say("Enter search words.", error=True)
            return False
        ok, data = await self.api(
            "GET",
            "/v1/memory/search",
            params={"q": text, "ns": self.val("ns"), "limit": 20},
        )
        if ok:
            self.fill_table(data.get("data", []))
            self.say(
                f"{len(data.get('data', []))} match(es); Refresh to see everything"
            )
        return False

    async def act_delete(self, row: dict) -> None:
        ok, data = await self.api(
            "DELETE",
            f"/v1/memory/{q(row['key'])}",
            params={"ns": row.get("ns", "default")},
        )
        if ok:
            self.say(f"deleted {data.get('deleted', 0)}")

    async def act_forget(self, row: Optional[dict]) -> None:
        ok, data = await self.api("DELETE", "/v1/memory", params={"ns": self.val("ns")})
        if ok:
            self.say(f"forgot {data.get('deleted', 0)} item(s)")


class RagPane(TablePane):
    TITLE = "RAG"
    HELP = "Document collections searched by embedding. Ingest reads a local file; Search shows the best chunks."
    ID_KEY = "source"
    FIELDS = [
        Field("coll", "collection", "default"),
        Field("file", "file to ingest (path)"),
        Field("query", "search query"),
        Field("k", "results", "4"),
    ]
    ACTIONS = [
        Action("ingest", "Ingest"),
        Action("search", "Search"),
        Action(
            "delete",
            "Delete",
            needs_row=True,
            confirm="Delete {source} and its chunks?",
            variant="warning",
        ),
    ]

    def coll(self) -> str:
        return self.val("coll") or "default"

    def list_path(self) -> str:
        return f"/v1/rag/{q(self.coll())}/documents"

    async def act_ingest(self, row: Optional[dict]) -> None:
        path = self.val("file")
        if not path:
            self.say("Enter a file path.", error=True)
            return
        try:
            text = await asyncio.to_thread(
                Path(path).expanduser().read_text, "utf-8", "replace"
            )
        except OSError as e:
            self.say(f"cannot read {path}: {e.strerror or e}", error=True)
            return
        if len(text) > MAX_RAG_CHARS:
            self.say(
                f"{path} has {len(text)} characters; the limit is {MAX_RAG_CHARS}.",
                error=True,
            )
            return
        ok, data = await self.api(
            "POST",
            f"/v1/rag/{q(self.coll())}/documents",
            json={"source": path, "text": text},
        )
        if ok:
            self.say(f"ingested {path}: {data.get('chunks', '?')} chunk(s)")

    async def act_search(self, row: Optional[dict]) -> bool:
        text = self.val("query")
        if not text:
            self.say("Enter a search query.", error=True)
            return False
        try:
            k = max(1, min(int(self.val("k") or 4), 20))
        except ValueError:
            self.say("results must be a whole number.", error=True)
            return False
        ok, data = await self.api(
            "POST", f"/v1/rag/{q(self.coll())}/search", json={"query": text, "k": k}
        )
        if ok:
            hits = data.get("data", [])
            self.fill_table(hits)
            self.say(f"{len(hits)} hit(s); Refresh to list sources again")
        return False

    async def act_delete(self, row: dict) -> None:
        # the route is `{source:path}`: keep slashes, encode everything else
        from urllib.parse import quote

        ok, data = await self.api(
            "DELETE",
            f"/v1/rag/{q(self.coll())}/documents/{quote(row['source'], safe='/')}",
        )
        if ok:
            self.say(
                f"deleted {data.get('deleted', '?')} ({data.get('chunks', 0)} chunks)"
            )


class FeedbackPane(TablePane):
    TITLE = "Feedback"
    HELP = "Ratings and corrections on responses. Corrections with prompts export as eval tasks."
    PATH = "/v1/feedback"
    ID_KEY = "request_id"
    COLUMNS = [
        ("Request", "request_id"),
        ("Rating", "rating"),
        ("Model", "model"),
        ("Comment", "comment"),
        ("Correction", "correction"),
    ]
    FIELDS = [
        Field("rating", "rating: 1 or -1 (also filters the list)"),
        Field("request", "request id"),
        Field("comment", "comment"),
        Field("prompt", "prompt"),
        Field("correction", "correction"),
        Field("model", "model"),
        Field("out", "eval tasks file for Export", "feedback-evals.jsonl"),
    ]
    ACTIONS = [
        Action("submit", "Submit"),
        Action("export", "Export"),
        Action(
            "delete",
            "Delete",
            needs_row=True,
            confirm="Delete feedback for {request_id}?",
            variant="warning",
        ),
    ]

    def params(self) -> dict:
        r = self.val("rating")
        return {"rating": r} if r in ("1", "-1") else {}

    async def fetch(self) -> Optional[list[dict]]:
        ok, data = await self.api("GET", self.list_path(), params=self.params())
        if not ok:
            return None
        self.summary = data.get("summary") or {}
        return data.get("data", [])

    summary: dict = {}

    def status_line(self, rows: list[dict]) -> str:
        return f"{len(rows)} row(s) · {self.summary.get('up', 0)} up / {self.summary.get('down', 0)} down overall"

    async def act_submit(self, row: Optional[dict]) -> None:
        rating = self.val("rating")
        if rating not in ("1", "-1") or not self.val("request"):
            self.say("Need a request id and a rating of 1 or -1.", error=True)
            return
        body = {
            "request_id": self.val("request"),
            "rating": int(rating),
            "comment": self.val("comment"),
            "prompt": self.val("prompt"),
            "correction": self.val("correction"),
            "model": self.val("model"),
        }
        ok, _ = await self.api("POST", "/v1/feedback", json=body)
        if ok:
            self.say(f"recorded {rating} for {body['request_id']}")

    async def act_export(self, row: Optional[dict]) -> bool:
        ok, data = await self.api("GET", "/v1/feedback/eval-tasks")
        if ok:
            tasks = data.get("tasks", [])
            path = Path(self.val("out") or "feedback-evals.jsonl")
            await asyncio.to_thread(
                path.write_text,
                "".join(json.dumps(t, ensure_ascii=False) + "\n" for t in tasks),
                "utf-8",
            )
            self.say(f"wrote {len(tasks)} task(s) to {path}")
        return False

    async def act_delete(self, row: dict) -> None:
        ok, _ = await self.api("DELETE", f"/v1/feedback/{q(row['request_id'])}")
        if ok:
            self.say(f"deleted {row['request_id']}")


# ------------------------------------------------------------ models / ops
class LocalPane(TablePane):
    TITLE = "Local models"
    HELP = "Managed local GGUF models (admin; needs config/local.yml). Pull can take minutes."
    PATH = "/v1/local/models"
    COLUMNS = None
    FIELDS = [Field("model", "org/repo:QUANT to pull")]
    ACTIONS = [
        Action("pull", "Pull", variant="success"),
        Action("inspect", "Inspect", needs_row=True),
        Action("unload", "Unload", needs_row=True),
        Action(
            "rm",
            "Delete",
            needs_row=True,
            confirm="Delete model {id} from disk?",
            variant="error",
        ),
    ]

    async def act_pull(self, row: Optional[dict]) -> None:
        model = self.val("model")
        if not model:
            self.say("Enter org/repo:QUANT.", error=True)
            return
        self.say(f"downloading {model} …")
        ok, data = await self.api(
            "POST", "/v1/local/models/pull", json={"model": model}, timeout=None
        )
        if ok:
            self.say(f"pulled {data.get('file', model)}")

    async def act_inspect(self, row: dict) -> bool:
        ok, data = await self.api("GET", f"/v1/local/models/{q(row['id'])}")
        if ok:
            self.render_detail(data)
        return False

    async def act_unload(self, row: dict) -> None:
        ok, data = await self.api("POST", f"/v1/local/models/{q(row['id'])}/unload")
        if ok:
            self.say("unloaded" if data.get("unloaded") else "was not loaded")

    async def act_rm(self, row: dict) -> None:
        ok, _ = await self.api("DELETE", f"/v1/local/models/{q(row['id'])}")
        if ok:
            self.say(f"deleted {row['id']}")


class FinetunePane(TablePane):
    TITLE = "Fine-tuning"
    HELP = 'Fine-tuning jobs (admin). Dataset: JSONL, one {"messages": [...]} per line. Estimate before submitting.'
    PATH = "/v1/finetune/jobs"
    ID_KEY = "job_id"
    REFRESH = 10
    COLUMNS = [
        ("Job", "job_id"),
        ("Status", "status"),
        ("Base", "base_model"),
        ("Provider", "provider"),
        ("Result", "fine_tuned_model"),
        ("Est. $", "estimated_usd"),
        ("Tokens", "trained_tokens"),
        ("Error", "error"),
    ]
    FIELDS = [
        Field("dataset", "dataset .jsonl path"),
        Field("base", "base model"),
        Field("provider", "provider", "openai"),
        Field("preset", "hyperparameter preset", "balanced"),
        Field("max_cost", "max cost USD (refuse above)"),
        Field("status", "status filter"),
    ]
    ACTIONS = [
        Action("estimate", "Estimate"),
        Action(
            "submit",
            "Submit",
            confirm="Submit this fine-tuning job? It may cost money (see the estimate).",
            variant="warning",
        ),
        Action("status", "Status", needs_row=True),
        Action(
            "cancel",
            "Cancel",
            needs_row=True,
            confirm="Cancel job {job_id}?",
            variant="error",
        ),
    ]

    def params(self) -> dict:
        return {"status": self.val("status")}

    def job_body(self) -> Optional[dict]:
        from caips_cli.cli import CliError

        path, base = self.val("dataset"), self.val("base")
        if not path or not base:
            self.say("Need a dataset path and a base model.", error=True)
            return None
        try:
            examples = read_jsonl(path)
        except CliError as e:
            self.say(str(e), error=True)
            return None
        return {
            "base_model": base,
            "provider": self.val("provider") or "openai",
            "hyperparam_preset": self.val("preset") or "balanced",
            "examples": examples,
        }

    async def act_estimate(self, row: Optional[dict]) -> bool:
        body = self.job_body()
        if body is not None:
            ok, data = await self.api("POST", "/v1/finetune/estimate", json=body)
            if ok:
                self.render_detail(data)
                self.say("estimate only; nothing was submitted")
        return False

    async def act_submit(self, row: Optional[dict]) -> None:
        body = self.job_body()
        if body is None:
            return
        if self.val("max_cost"):
            try:
                body["max_cost_usd"] = float(self.val("max_cost"))
            except ValueError:
                self.say("max cost must be a number.", error=True)
                return
        ok, data = await self.api("POST", "/v1/finetune/jobs", json=body)
        if ok:
            self.say(f"submitted {data.get('job_id', '?')}")

    async def act_status(self, row: dict) -> bool:
        ok, data = await self.api("GET", f"/v1/finetune/jobs/{q(row['job_id'])}")
        if ok:
            self.render_detail(data)
        return False

    async def act_cancel(self, row: dict) -> None:
        ok, _ = await self.api("DELETE", f"/v1/finetune/jobs/{q(row['job_id'])}")
        if ok:
            self.say(f"cancelled {row['job_id']}")


class AdminPane(TablePane):
    TITLE = "Admin"
    HELP = "Audit log, kill switch and emergency shutdown (admin). Shutdown stops the server until restarted."
    PATH = "/v1/admin/audit"
    REFRESH = 10
    ID_KEY = "_i"
    FIELDS = [
        Field("limit", "rows (1-1000)", "100"),
        Field("action", "action filter (e.g. tool)"),
        Field("actor", "actor filter"),
        Field("mode", "shutdown mode: graceful | immediate | forced", "graceful"),
        Field("message", "shutdown message"),
        Field("owner", "owner for Kill runs (blank = everyone)"),
    ]
    ACTIONS = [
        Action(
            "kill",
            "Kill runs",
            confirm="Stop ALL active agent runs (or only the owner given)?",
            variant="warning",
        ),
        Action(
            "shutdown",
            "Shutdown",
            confirm="EMERGENCY SHUTDOWN of the gateway. It will stop serving until someone restarts it. Continue?",
            variant="error",
        ),
    ]

    def params(self) -> dict:
        return {
            "limit": self.val("limit") or "100",
            "action": self.val("action"),
            "actor": self.val("actor"),
        }

    def unwrap(self, data: Any) -> list[dict]:
        rows = data.get("events", []) if isinstance(data, dict) else []
        for i, r in enumerate(rows):
            r["_i"] = i
        return rows

    def columns_for(self, rows: list[dict]) -> list[tuple[str, str]]:
        cols = super().columns_for(rows)
        return [c for c in cols if c[0] != "_i"]

    async def act_kill(self, row: Optional[dict]) -> None:
        ok, data = await self.api(
            "POST", "/v1/runs/kill", json={"owner": self.val("owner") or None}
        )
        if ok:
            self.say(f"killed {data.get('killed', 0)} run(s)")

    async def act_shutdown(self, row: Optional[dict]) -> bool:
        mode = self.val("mode") or "graceful"
        if mode not in ("graceful", "immediate", "forced"):
            self.say("mode must be graceful, immediate or forced.", error=True)
            return False
        ok, data = await self.api(
            "POST",
            "/v1/admin/shutdown",
            params={"mode": mode, "message": self.val("message")},
        )
        if ok:
            self.render_detail(data)
            self.say(f"shutdown initiated ({mode})")
        return False


class EvalsPane(Pane):
    """Runs ``python -m evals`` against the gateway and streams its output."""

    TITLE = "Evals"
    HELP = "Run an eval suite through the gateway (docs/evals.md). Exit code 1 = regression against the baseline."
    FIELDS = [
        Field("tasks", "tasks directory or file", "evals/"),
        Field("model", "model or alias (blank = default)"),
        Field("baseline", "baseline results .json (optional)"),
        Field("out", "results file", "eval-results.json"),
    ]

    def __init__(self, *a: Any, **kw: Any):
        super().__init__(*a, **kw)
        self.proc: Optional[asyncio.subprocess.Process] = None

    def body(self) -> ComposeResult:
        yield from self.inputs(self.FIELDS)
        with Grid(classes="actions"):
            yield Button("Run", id="run", variant="success")
            yield Button("Stop", id="stop", variant="error")
        yield RichLog(classes="detail wide", wrap=True, markup=False, highlight=False)

    def command(self) -> list[str]:
        cmd = [
            sys.executable,
            "-m",
            "evals",
            self.val("tasks"),
            "--base-url",
            self.client.base_url,
            "--out",
            self.val("out") or "eval-results.json",
        ]
        if self.val("model"):
            cmd += ["--model", self.val("model")]
        if self.val("baseline"):
            cmd += ["--baseline", self.val("baseline")]
        return cmd

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "run":
            self.run_worker(self.run_suite(), group="evals", exclusive=True)
        elif event.button.id == "stop":
            self.stop()

    def stop(self) -> None:
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            self.say("stopping …")

    async def run_suite(self) -> int:
        if self.proc and self.proc.returncode is None:
            self.say("An eval run is already in progress.", error=True)
            return -1
        if not self.val("tasks"):
            self.say("Enter the tasks path.", error=True)
            return -1
        log = self.detail_log()
        log.clear()
        env = dict(os.environ)
        src = str(Path(__file__).resolve().parents[1])
        env["PYTHONPATH"] = os.pathsep.join(
            p for p in (src, env.get("PYTHONPATH", "")) if p
        )
        if self.client.api_key:
            env["CAIPS_API_KEY"] = (
                self.client.api_key
            )  # via the environment, never argv
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *self.command(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
        except OSError as e:
            self.say(f"cannot start evals: {e}", error=True)
            return -1
        self.say("running …")
        assert self.proc.stdout is not None
        async for line in self.proc.stdout:
            log.write(Text(line.decode("utf-8", "replace").rstrip("\n")))
        code = await self.proc.wait()
        self.say(
            f"finished with exit code {code}"
            + (" (regression or failure)" if code else ""),
            error=bool(code),
        )
        return code
