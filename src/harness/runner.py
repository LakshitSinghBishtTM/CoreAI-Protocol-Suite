"""harness/runner.py: the tool-use loop.

plan -> call model -> run tools -> observe -> repeat, until the model answers
or a limit trips. State is checkpointed after every step, so a crashed,
paused or approval-blocked run resumes where it stopped.

``route`` is any async ``CompletionRequest -> CompletionResponse`` (normally
``router.route``). Budgets are also passed to the gateway in request metadata;
the harness limits here are a second, independent guard.
"""

import asyncio
import json
import time
from typing import Awaitable, Callable, Optional

from providers.base import CompletionRequest, CompletionResponse, Message
from tools.executor import _run_one
from tools.registry import Permission, ToolRegistry
from utils.events import bus

from . import context
from .config import AgentDef
from .store import RunStore


REVIEW_TOOL = "request_human_review"
REVIEW_SPEC = {
    "type": "function",
    "function": {
        "name": REVIEW_TOOL,
        "description": (
            "Pause and ask a human before continuing. Use when you are unsure "
            "or an action is risky. Give your question and what you would do."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "proposed_action": {"type": "string"},
            },
            "required": ["question"],
        },
    },
}


def _finished(run: dict) -> bool:
    """Completed, cancelled or limit-stopped runs are final; an error stop can be resumed."""
    return run["status"] in ("completed", "cancelled") or (
        run["status"] == "failed" and run["stop_reason"] != "error"
    )


PAUSE_REASONS = {"pause": "paused", "cancel": "cancelled", "kill": "killed"}


class HarnessRunner:
    def __init__(
        self,
        route: Callable[[CompletionRequest], Awaitable[CompletionResponse]],
        registry: ToolRegistry,
        store: RunStore,
        agents: Optional[dict[str, AgentDef]] = None,
        caller: str = "agent",
        max_permission: Permission = Permission.DESTRUCTIVE,
        memory=None,
    ):
        self.memory = memory  # optional memory.MemoryStore; read-only here
        self.route, self.registry, self.store = route, registry, store
        self.agents = agents or {}
        self.caller, self.max_permission = caller, max_permission

    # -- run API ---------------------------------------------------------
    def create(self, agent: str, task: str, owner: str = "local") -> str:
        a = self._agent(agent)
        deadline = time.time() + a.deadline_s if a.deadline_s else None
        rid = self.store.create(agent, owner, [Message("user", task)], deadline)
        self.store.event(rid, "run_started", agent=agent, owner=owner)
        return rid

    async def run(self, agent: str, task: str, owner: str = "local") -> dict:
        rid = self.create(agent, task, owner)
        return await self.execute(rid)

    def status(self, rid: str) -> Optional[dict]:
        r = self.store.get(rid)
        if r:
            r.pop("messages", None)
            r["pending"] = json.loads(r["pending"]) if r["pending"] else None
        return r

    def cancel(self, rid: str) -> bool:
        r = self.store.get(rid)
        if r and r["status"] in (
            "paused",
            "waiting_approval",
        ):  # nothing running: finish now
            self._finish(rid, "cancelled", "cancelled")
            return True
        return self.store.request(rid, "cancel")

    def pause(self, rid: str) -> bool:
        return self.store.request(rid, "pause")

    def kill(self, owner: Optional[str] = None) -> int:
        """Stop all runs for an owner, or the whole instance. Idle runs end now."""
        n = self.store.kill(owner)
        for r in self.store.list_runs(owner=owner):
            if r["status"] in ("paused", "waiting_approval"):
                self._finish(r["id"], "cancelled", "killed")
        return n

    async def resume(self, rid: str, approve: Optional[bool] = None) -> dict:
        """Continue a paused, approval-blocked or crash-interrupted run."""
        r = self.store.get(rid)
        if r is None:
            raise KeyError(rid)
        if r["status"] == "waiting_approval" and approve is None:
            raise ValueError("run is waiting for an approval decision")
        return await self.execute(rid, decision=approve)

    async def stream(self, rid: str, poll: float = 0.05):
        """Yield trace events as they happen, until the run ends or blocks."""
        seen = 0
        while True:
            for e in self.store.events(rid, seen):
                seen = e["seq"]
                yield e
            r = self.store.get(rid)
            if r is None or r["status"] != "running":
                for e in self.store.events(rid, seen):
                    yield e
                return
            await asyncio.sleep(poll)

    # -- the loop --------------------------------------------------------
    async def execute(self, rid: str, decision: Optional[bool] = None) -> dict:
        run = self.store.get(rid)
        if run is None:
            raise KeyError(rid)
        if _finished(run):
            return self.status(rid)
        agent = self._agent(run["agent"]).effective()
        msgs = self.store.messages(rid)
        state = {
            k: run[k] for k in ("step", "cost_usd", "input_tokens", "output_tokens")
        }
        pending = json.loads(run["pending"]) if run["pending"] else None
        self.store.set(rid, status="running", pending=None)
        self.store.event(
            rid, "resumed" if run["step"] or pending else "started", step=state["step"]
        )

        try:
            if pending:  # finish the tool calls the human was asked about
                stop = await self._tools(
                    rid, agent, msgs, pending["calls"], state, decision
                )
                if stop:
                    return self.status(rid)
            while True:
                reason = self._limit(rid, agent, state)
                if reason:
                    return self._end(rid, msgs, state, reason)
                resp = await self._model(rid, agent, msgs, state)
                state["step"] += 1  # counts completed model calls only
                calls = (resp.metadata or {}).get("tool_calls")
                if not calls:
                    msgs.append(Message("assistant", resp.content or ""))
                    self._checkpoint(rid, msgs, state, result=resp.content)
                    return self._end(rid, msgs, state, "completed", result=resp.content)
                msgs.append(Message("assistant", resp.content or "", tool_calls=calls))
                if await self._tools(rid, agent, msgs, calls, state, None):
                    return self.status(rid)
                self._checkpoint(rid, msgs, state)
                if self._stalled(msgs, agent):
                    return self._end(rid, msgs, state, "stalled")
        except asyncio.CancelledError:
            self._checkpoint(
                rid, msgs, state, status="paused", stop_reason="shutdown"
            )  # resumable
            raise
        except Exception as exc:  # model/gateway failure: keep state, report why
            self.store.event(
                rid, "error", error=type(exc).__name__, detail=str(exc)[:300]
            )
            self._checkpoint(rid, msgs, state)
            self.store.set(rid, status="failed", stop_reason="error")
            self.store.event(rid, "run_finished", reason="error", step=state["step"])
            return self.status(rid)

    def _system(self, rid, agent: AgentDef, msgs) -> str:
        """System prompt plus recalled memories (owner-scoped, labeled untrusted)."""
        if not (self.memory and agent.memory_ns and agent.memory_recall > 0):
            return agent.system_prompt
        task = next((m.content for m in msgs if m.role == "user" and m.content), "")
        owner = self.store.get(rid)["owner"]
        found = self.memory.search(
            owner, str(task), ns=agent.memory_ns, limit=agent.memory_recall
        )
        if not found:
            return agent.system_prompt
        from guardrails.injection import wrap_untrusted

        text = "\n".join(f"- {m['key']}: {m['value']}" for m in found)
        return (
            agent.system_prompt
            + "\n\nRelevant stored memories:\n"
            + wrap_untrusted(text, f"memory:{agent.memory_ns}", kind="memory")
        )

    async def _model(self, rid, agent: AgentDef, msgs, state) -> CompletionResponse:
        system = self._system(rid, agent, msgs)
        summarizer = self._summarizer(agent) if agent.summarize else None
        ctx, changed = await context.compact(
            msgs, system=system, max_tokens=agent.context_tokens,
            keep_recent=agent.keep_recent, summarizer=summarizer,
        )  # fmt: skip
        if changed:
            msgs[:] = ctx  # compaction is part of durable state
            self.store.event(
                rid,
                "context_compacted",
                messages=len(ctx),
                tokens=context.count(ctx, system),
            )
        meta = {
            "run_id": rid,
            "agent": agent.name,
            "owner": self.store.get(rid)["owner"],
        }
        if agent.max_cost_usd is not None:
            meta["budget_remaining_usd"] = max(
                0.0, agent.max_cost_usd - state["cost_usd"]
            )
        req = CompletionRequest(
            messages=list(ctx), model=agent.model, max_tokens=agent.max_tokens,
            temperature=agent.temperature, system_prompt=system,
            tools=(
                self.registry.specs(agent.allowed_tools)
                + ([REVIEW_SPEC] if agent.ask_when_uncertain else [])
            ) or None, metadata=meta,
        )  # fmt: skip
        run = self.store.get(rid)
        timeout = max(0.1, run["deadline"] - time.time()) if run["deadline"] else None
        t0 = time.time()
        resp = await asyncio.wait_for(self.route(req), timeout)
        state["cost_usd"] += resp.cost_usd or 0.0
        state["input_tokens"] += resp.input_tokens or 0
        state["output_tokens"] += resp.output_tokens or 0
        self.store.event(
            rid, "model_call", step=state["step"] + 1, model=resp.model, provider=resp.provider,
            input_tokens=resp.input_tokens, output_tokens=resp.output_tokens,
            cost_usd=resp.cost_usd, ms=round((time.time() - t0) * 1000, 1),
            tool_calls=len((resp.metadata or {}).get("tool_calls") or []),
        )  # fmt: skip
        return resp

    async def _tools(self, rid, agent, msgs, calls, state, decision) -> bool:
        """Run calls in order. True means the run stopped waiting for a human."""
        for i, call in enumerate(calls):
            name = call.get("name", "")
            first = decision is not None and i == 0
            if name == REVIEW_TOOL and agent.ask_when_uncertain:
                if not first:
                    return self._wait(rid, msgs, state, calls[i:])
                self.store.event(rid, "approval", tool=name, approved=decision)
                msgs.append(
                    Message(
                        "tool",
                        "Human reviewer approved: proceed."
                        if decision
                        else "Human reviewer declined; choose another approach.",
                        tool_call_id=call.get("id", ""),
                    )
                )
                decision = None
                continue
            forced = name in agent.approval_tools
            if forced and not first:
                return self._wait(rid, msgs, state, calls[i:])
            if first and decision is False:
                step_out, ok = (
                    "Denied by human reviewer; do not retry this call.",
                    False,
                )
                self.store.event(rid, "approval", tool=name, approved=False)
            else:
                allow = (lambda c, n, a: True) if first else (lambda c, n, a: False)
                step = await _run_one(
                    self.registry,
                    call,
                    agent.allowed_tools,
                    self.caller,
                    allow,
                    self.max_permission,
                )
                if step.error_code == "approval_required":
                    return self._wait(rid, msgs, state, calls[i:])
                step_out, ok = step.output, step.ok
                if first:
                    self.store.event(rid, "approval", tool=name, approved=True)
                self.store.event(
                    rid, "tool_call", step=state["step"], tool=name, ok=ok,
                    args=sorted(step.arguments), ms=step.duration_ms,
                    chars=len(step_out), error=step.error_code,
                )  # fmt: skip
            if first and decision is False:
                self.store.event(
                    rid,
                    "tool_call",
                    step=state["step"],
                    tool=name,
                    ok=False,
                    error="denied",
                )
            msgs.append(Message("tool", step_out, tool_call_id=call.get("id", "")))
            decision = None
        return False

    def _wait(self, rid, msgs, state, calls) -> bool:
        self._checkpoint(
            rid, msgs, state, status="waiting_approval", pending={"calls": calls}
        )
        self.store.event(
            rid,
            "approval_requested",
            tool=calls[0].get("name"),
            call_id=calls[0].get("id"),
        )
        return True

    # -- limits, stall, bookkeeping -------------------------------------
    def _limit(self, rid, agent: AgentDef, st) -> Optional[str]:
        run = self.store.get(rid)
        if run["control"] in PAUSE_REASONS:
            return PAUSE_REASONS[run["control"]]
        if run["deadline"] and time.time() >= run["deadline"]:
            return "deadline"
        if st["step"] >= agent.max_steps:
            return "max_steps"
        if agent.max_cost_usd is not None and st["cost_usd"] >= agent.max_cost_usd:
            return "cost_limit"
        if (
            agent.max_total_tokens
            and st["input_tokens"] + st["output_tokens"] >= agent.max_total_tokens
        ):
            return "token_limit"
        return None

    @staticmethod
    def _stalled(msgs, agent: AgentDef) -> bool:
        n = agent.stall_repeats
        sigs = [
            tuple((c.get("name"), c.get("arguments")) for c in m.tool_calls)
            for m in msgs if m.role == "assistant" and m.tool_calls
        ]  # fmt: skip
        return n > 1 and len(sigs) >= n and len(set(sigs[-n:])) == 1

    def _checkpoint(self, rid, msgs, state, **extra):
        self.store.checkpoint(rid, msgs, **state, **extra)
        self.store.event(rid, "checkpoint", step=state["step"])

    def _end(self, rid, msgs, state, reason, result=None) -> dict:
        if reason == "paused":
            self._checkpoint(
                rid, msgs, state, status="paused", control="", stop_reason="paused"
            )
            self.store.event(rid, "paused", step=state["step"])
            return self.status(rid)
        status = {
            "completed": "completed",
            "cancelled": "cancelled",
            "killed": "cancelled",
        }.get(reason, "failed")
        self._checkpoint(
            rid,
            msgs,
            state,
            status=status,
            stop_reason=reason,
            control="",
            result=result,
        )
        self.store.event(
            rid,
            "run_finished",
            reason=reason,
            step=state["step"],
            cost_usd=round(state["cost_usd"], 6),
        )
        self._notify_finished(rid, status, reason, state["cost_usd"])
        return self.status(rid)

    def _finish(self, rid, status, reason):
        self.store.set(rid, status=status, stop_reason=reason, control="")
        self.store.event(rid, "run_finished", reason=reason)
        self._notify_finished(rid, status, reason, None)

    def _notify_finished(self, rid, status, reason, cost):
        run = self.store.get(rid) or {}
        bus.emit(
            "run.finished", run_id=rid, agent=run.get("agent"), owner=run.get("owner"),
            status=status, reason=reason,
            cost_usd=round(cost if cost is not None else run.get("cost_usd") or 0.0, 6),
        )  # fmt: skip

    def _agent(self, name: str) -> AgentDef:
        try:
            return self.agents[name]
        except KeyError:
            raise KeyError(f"unknown agent: {name}") from None

    def _summarizer(self, agent: AgentDef):
        async def summarize(dropped: list[Message]) -> str:
            text = "\n".join(f"{m.role}: {(m.content or '')[:400]}" for m in dropped)
            req = CompletionRequest(
                messages=[Message("user", "Summarize these earlier agent steps in under 120 words, "
                                          "keeping facts needed to finish the task:\n" + text)],
                model=agent.model, max_tokens=200, temperature=0.0,
            )  # fmt: skip
            return (await self.route(req)).content

        return summarize
