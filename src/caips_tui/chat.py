"""Chat over the gateway, in any of its three dialects, with streaming."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, Input, Select, Static

from caips_tui.client import ApiError, pretty
from caips_tui.widgets import Pane

HELP_TEXT = """Commands
  /clear             start a new conversation
  /retry             ask the last question again
  /explain [text]    dry-run routing for the conversation (nothing is sent to a provider)
  /good [comment]    rate the last reply 👍 (sends feedback)
  /bad [correction]  rate the last reply 👎; optional text is the answer you wanted
  /save <path>       write the transcript as Markdown
  /system <text>  /model <name>  /provider <name>  /policy <name>
  /mode chat|native|anthropic     /stream on|off
Esc stops a reply in progress."""

MODES = [
    ("OpenAI chat", "chat"),
    ("CaiPS native", "native"),
    ("Anthropic", "anthropic"),
]
MODE_PATHS = {
    "chat": "/v1/chat/completions",
    "native": "/v1/completions",
    "anthropic": "/v1/messages",
}
FLAGS = {"stream": "stream", "tools": "server tools"}  # flag name -> button text


def meta_line(meta: dict, ttft: Optional[float], total: float) -> str:
    parts = [meta.get("provider"), meta.get("model")]
    if meta.get("input_tokens") is not None or meta.get("output_tokens") is not None:
        parts.append(
            f"{meta.get('input_tokens', '?')} in / {meta.get('output_tokens', '?')} out tokens"
        )
    if meta.get("cost_usd") is not None:
        parts.append(f"${float(meta['cost_usd']):.6f}")
    if meta.get("cached"):
        parts.append("cached")
    if ttft is not None:
        parts.append(f"first token {ttft * 1000:.0f} ms")
    parts.append(f"total {total * 1000:.0f} ms")
    return " · ".join(str(p) for p in parts if p)


class ChatPane(Pane):
    FLAGS = FLAGS
    TITLE = "API"
    HELP = "Raw API playground · the dropdown picks the dialect · Enter sends · Esc stops · /help"
    BINDINGS = [Binding("escape", "stop", "Stop reply", show=False)]

    def __init__(self, *a: Any, **kw: Any):
        super().__init__(*a, **kw)
        self.history: list[dict] = []
        # request_id / prompt / model of the latest reply, for feedback
        self.last: dict = {}
        self.busy = False
        # (prompt, history length before it), so /retry works after a failure too
        self.attempt: Optional[tuple[str, int]] = None
        self.flags = {"stream": True, "tools": False}

    # -- layout ---------------------------------------------------------
    def body(self) -> ComposeResult:
        with Horizontal(classes="row"):
            yield Select(MODES, value="chat", allow_blank=False, id="mode")
            yield Input(placeholder="model (blank = router picks)", id="fld-model")
            yield Input(placeholder="provider", id="fld-provider")
            yield Input(placeholder="routing policy", id="fld-policy")
        with Horizontal(classes="row"):
            yield Input(placeholder="temperature", value="0.7", id="fld-temperature")
            yield Input(placeholder="max tokens", value="1024", id="fld-max_tokens")
            yield Input(
                placeholder="system prompt (optional)", id="fld-system", classes="wide"
            )
        yield VerticalScroll(id="transcript")
        with Horizontal(classes="bar"):
            yield Button("Stop", id="stop", variant="warning")
            yield Button("Clear", id="clear")
            yield Button("Explain", id="explain")
            yield Button("Good", id="good", variant="success")
            yield Button("Bad", id="bad", variant="error")
            for name in self.FLAGS:
                yield Button(
                    self.flag_label(name),
                    id=f"tg-{name}",
                    variant="success" if self.flags[name] else "default",
                )
        yield Input(placeholder="Message …  (/help for commands)", id="prompt")

    def flag_label(self, name: str) -> str:
        return f"{self.FLAGS[name]}: {'on' if self.flags[name] else 'off'}"

    def set_flag(self, name: str, on: bool) -> None:
        self.flags[name] = on
        btn = self.query_one(f"#tg-{name}", Button)
        btn.label = self.flag_label(name)
        btn.variant = "success" if on else "default"

    def focus_primary(self) -> None:
        self.query_one("#prompt", Input).focus()

    @property
    def mode(self) -> str:
        try:
            v = self.query_one("#mode", Select).value
        except Exception:  # panes without a dialect dropdown always speak OpenAI chat
            return "chat"
        return v if isinstance(v, str) else "chat"

    # -- hooks for subclasses -------------------------------------------
    def window(self) -> list[dict]:
        """The messages sent to the model (all of the history by default)."""
        return list(self.history)

    def extra_body(self) -> dict:
        """Extra fields merged into an OpenAI-chat request body."""
        return {}

    # -- transcript -----------------------------------------------------
    async def add(self, kind: str, text: str) -> Static:
        w = Static(Text(text), classes=f"msg {kind}")
        t = self.query_one("#transcript", VerticalScroll)
        await t.mount(w)
        t.scroll_end(animate=False)
        return w

    async def note(self, text: str, error: bool = False) -> None:
        await self.add("error" if error else "info", text)

    async def clear(self) -> None:
        self.history.clear()
        self.last = {}
        self.attempt = None
        await self.query_one("#transcript", VerticalScroll).remove_children()
        self.say("new conversation")

    # -- input handling -------------------------------------------------
    async def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        if event.input.id != "prompt":
            return
        text = event.input.value.strip()
        if not text:
            return
        event.input.value = ""
        if text.startswith("/"):
            await self.command(text)
        elif self.busy:
            await self.note("Still replying; press Esc to stop it first.", error=True)
        else:
            self.run_worker(self.send(text), group="chat", exclusive=True)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        bid = event.button.id
        if bid == "stop":
            self.action_stop()
        elif bid == "clear":
            await self.clear()
        elif bid and bid.startswith("tg-"):
            self.set_flag(bid[3:], not self.flags[bid[3:]])
        elif bid == "explain":
            await self.explain("")
        elif bid in ("good", "bad"):
            await self.feedback(1 if bid == "good" else -1, "")

    def action_stop(self) -> None:
        self.workers.cancel_group(self, "chat")

    async def command(self, line: str) -> None:
        cmd, _, arg = line[1:].partition(" ")
        arg = arg.strip()
        setters = {
            "system": "system",
            "model": "model",
            "provider": "provider",
            "policy": "policy",
        }
        if cmd == "help":
            await self.note(HELP_TEXT)
        elif cmd == "clear":
            await self.clear()
        elif cmd == "retry":
            await self.retry()
        elif cmd == "explain":
            await self.explain(arg)
        elif cmd in ("good", "bad"):
            await self.feedback(1 if cmd == "good" else -1, arg)
        elif cmd == "save":
            await self.save(arg)
        elif cmd in setters:
            self.set_val(setters[cmd], arg)
            await self.note(f"{cmd} = {arg or '(cleared)'}")
        elif cmd == "mode" and arg in ("chat", "native", "anthropic"):
            try:
                self.query_one("#mode", Select).value = arg
            except Exception:
                await self.note(
                    "This view always uses the OpenAI chat dialect.", error=True
                )
                return
            await self.note(f"mode = {arg}")
        elif cmd == "stream" and arg in ("on", "off"):
            self.set_flag("stream", arg == "on")
            await self.note(f"stream = {arg}")
        else:
            await self.note(f"Unknown command: {line}. Try /help.", error=True)

    # -- requests -------------------------------------------------------
    def settings(self) -> Optional[dict]:
        """Validated knobs from the inputs, or ``None`` (after reporting) if one is invalid."""
        try:
            temp = float(self.val("temperature")) if self.val("temperature") else None
            max_tokens = int(self.val("max_tokens")) if self.val("max_tokens") else None
        except ValueError:
            self.say(
                "temperature must be a number and max tokens a whole number.",
                error=True,
            )
            return None
        return {
            "model": self.val("model") or None,
            "provider": self.val("provider") or None,
            "policy": self.val("policy") or None,
            "system": self.val("system") or None,
            "temperature": temp,
            "max_tokens": max_tokens,
        }

    async def generate(self, s: dict, stream: bool) -> AsyncIterator[tuple[str, Any]]:
        """Yield ``("delta", text)`` and ``("meta", dict)`` for the selected dialect."""
        mode = self.mode
        if mode == "chat":
            msgs = (
                [{"role": "system", "content": s["system"]}] if s["system"] else []
            ) + self.window()
            body: dict[str, Any] = {"messages": msgs, "stream": stream}
            body.update(self.extra_body())
            for k in ("model", "provider", "policy", "temperature", "max_tokens"):
                if s[k] is not None:
                    body[k] = s[k]
            if self.flags["tools"]:
                body["run_tools"] = True
            if stream:
                body["stream_options"] = {"include_usage": True}
                async for item in self._sse_chat(body):
                    yield item
            else:
                hdrs: dict = {}
                obj = await self.client.request(
                    "POST",
                    "/v1/chat/completions",
                    json=body,
                    on_headers=lambda h: hdrs.update(h),
                )
                msg = (obj.get("choices") or [{}])[0].get("message") or {}
                yield "delta", msg.get("content") or ""
                yield "meta", self._chat_meta(obj, hdrs)
        elif mode == "native":
            body = {
                "messages": self.window(),
                "max_tokens": s["max_tokens"] or 1024,
                "temperature": (
                    s["temperature"] if s["temperature"] is not None else 0.7
                ),
            }
            for src, dst in (
                ("model", "model"),
                ("provider", "provider"),
                ("policy", "policy"),
                ("system", "system_prompt"),
            ):
                if s[src]:
                    body[dst] = s[src]
            if stream:
                body["stream"] = True
                async for ev, data in self.client.stream(
                    "POST", "/v1/completions/stream", json=body
                ):
                    if data == "[DONE]":
                        break
                    obj = json.loads(data)
                    if ev == "error":
                        raise ApiError(0, f"stream error: {obj.get('error', data)}")
                    yield "delta", obj.get("content", "")
            else:
                r = await self.client.request("POST", "/v1/completions", json=body)
                yield "delta", r.get("content", "")
                yield "meta", {
                    k: r.get(k)
                    for k in (
                        "provider",
                        "model",
                        "input_tokens",
                        "output_tokens",
                        "cost_usd",
                        "cached",
                        "request_id",
                    )
                }
        else:  # anthropic
            body = {
                "messages": self.window(),
                "max_tokens": s["max_tokens"] or 1024,
                "stream": stream,
            }
            if s["system"]:
                body["system"] = s["system"]
            if s["model"]:
                body["model"] = s["model"]
            if s["provider"]:
                body["provider"] = s["provider"]
            if s["temperature"] is not None:
                body["temperature"] = min(
                    s["temperature"], 1.0
                )  # Anthropic's range is 0-1
            if stream:
                usage: dict = {}
                async for ev, data in self.client.stream(
                    "POST", "/v1/messages", json=body
                ):
                    obj = json.loads(data)
                    kind = obj.get("type", ev)
                    if kind == "error":
                        raise ApiError(0, (obj.get("error") or {}).get("message", data))
                    if kind == "message_start":
                        m = obj.get("message") or {}
                        usage.update(m.get("usage") or {})
                        yield "meta", {
                            "model": m.get("model"),
                            "request_id": m.get("id"),
                        }
                    elif kind == "content_block_delta":
                        yield "delta", (obj.get("delta") or {}).get("text", "")
                    elif kind == "message_delta":
                        usage.update(obj.get("usage") or {})
                yield "meta", {
                    "input_tokens": usage.get("input_tokens"),
                    "output_tokens": usage.get("output_tokens"),
                }
            else:
                r = await self.client.request("POST", "/v1/messages", json=body)
                yield "delta", "".join(
                    b.get("text", "")
                    for b in r.get("content", [])
                    if b.get("type") == "text"
                )
                u = r.get("usage") or {}
                yield "meta", {
                    "model": r.get("model"),
                    "request_id": r.get("id"),
                    "input_tokens": u.get("input_tokens"),
                    "output_tokens": u.get("output_tokens"),
                }

    async def _sse_chat(self, body: dict) -> AsyncIterator[tuple[str, Any]]:
        hdrs: dict = {}
        async for ev, data in self.client.stream(
            "POST",
            "/v1/chat/completions",
            json=body,
            on_headers=lambda h: hdrs.update(h),
        ):
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except ValueError:
                continue
            if ev == "error" or "error" in obj:
                err = obj.get("error")
                raise ApiError(
                    0, err.get("message", err) if isinstance(err, dict) else err or data
                )
            for ch in obj.get("choices") or []:
                piece = (ch.get("delta") or {}).get("content")
                if piece:
                    yield "delta", piece
            yield "meta", self._chat_meta(obj, hdrs)

    @staticmethod
    def _chat_meta(obj: dict, hdrs: dict) -> dict:
        u = obj.get("usage") or {}
        cost = hdrs.get("x-caips-cost-usd")
        return {
            "request_id": obj.get("id"),
            "model": obj.get("model"),
            "provider": hdrs.get("x-caips-provider"),
            "cost_usd": float(cost) if cost not in (None, "") else None,
            "input_tokens": u.get("prompt_tokens"),
            "output_tokens": u.get("completion_tokens"),
        }

    async def send(self, text: str) -> None:
        s = self.settings()
        if s is None or self.busy:
            return
        self.busy = True
        prompt_len = len(self.history)
        self.attempt = (text, prompt_len)
        self.history.append({"role": "user", "content": text})
        await self.add("user", text)
        widget = await self.add("assistant", "…")
        stream = self.flags["stream"]
        t0, ttft, buf, meta, shown = time.monotonic(), None, "", {}, 0.0
        try:
            async for kind, val in self.generate(s, stream):
                if kind == "delta":
                    if val and ttft is None:
                        ttft = time.monotonic() - t0
                    buf += val
                    if time.monotonic() - shown > 0.05:
                        widget.update(Text(buf))
                        self.query_one("#transcript", VerticalScroll).scroll_end(
                            animate=False
                        )
                        shown = time.monotonic()
                else:
                    meta.update({k: v for k, v in val.items() if v is not None})
            widget.update(Text(buf or "(empty reply)"))
            self.history.append({"role": "assistant", "content": buf})
            rid = meta.get("request_id") or f"tui-{uuid.uuid4().hex[:12]}"
            self.last = {
                "request_id": rid,
                "prompt": text,
                "model": meta.get("model") or s["model"] or "",
            }
            line = meta_line(meta, ttft, time.monotonic() - t0)
            await self.add("meta", line)
            self.say(line)
        except asyncio.CancelledError:
            widget.update(Text((buf + "\n" if buf else "") + "(stopped)"))
            del self.history[prompt_len:]
            raise
        except ApiError as e:
            widget.update(Text((buf + "\n" if buf else "") + f"Error: {e}"))
            widget.add_class("error")
            del self.history[prompt_len:]
            self.say(str(e), error=True)
        finally:
            self.busy = False
            self.query_one("#transcript", VerticalScroll).scroll_end(animate=False)

    async def retry(self) -> None:
        """Ask the last question again, whether its reply failed, was stopped, or just was not good enough."""
        if self.attempt is None or self.busy:
            await self.note(
                "Nothing to retry." if self.attempt is None else "Still replying.",
                error=True,
            )
            return
        text, before = self.attempt
        del self.history[
            before:
        ]  # drops the earlier reply if there was one; no-op after a failure
        self.run_worker(self.send(text), group="chat", exclusive=True)

    async def explain(self, draft: str) -> None:
        msgs = self.window()
        if draft:
            msgs.append({"role": "user", "content": draft})
        if not msgs:
            await self.note("Type a message first, or use /explain <text>.", error=True)
            return
        s = self.settings()
        if s is None:
            return
        body: dict[str, Any] = {"messages": msgs, "max_tokens": s["max_tokens"] or 1024}
        for src, dst in (
            ("model", "model"),
            ("provider", "provider"),
            ("policy", "policy"),
            ("system", "system_prompt"),
        ):
            if s[src]:
                body[dst] = s[src]
        ok, data = await self.api("POST", "/v1/routing/explain", json=body)
        if ok:
            await self.note("Routing (dry run)\n" + pretty(data))

    async def feedback(self, rating: int, text: str) -> None:
        if not self.last:
            await self.note("Nothing to rate yet.", error=True)
            return
        body = {
            "request_id": self.last["request_id"],
            "rating": rating,
            "comment": text if rating > 0 else "",
            "correction": text if rating < 0 else "",
            "prompt": self.last["prompt"],
            "model": self.last["model"],
        }
        ok, _ = await self.api("POST", "/v1/feedback", json=body)
        if ok:
            await self.note(
                "Thanks, feedback recorded."
                if rating > 0
                else "Recorded. Corrections become eval tasks."
            )

    async def save(self, path: str) -> None:
        if not path:
            await self.note("Usage: /save <path>", error=True)
            return
        lines = [f"**{m['role']}**\n\n{m['content']}\n" for m in self.history]
        try:
            await asyncio.to_thread(
                Path(path).expanduser().write_text, "\n".join(lines), "utf-8"
            )
        except OSError as e:
            await self.note(f"cannot write {path}: {e.strerror or e}", error=True)
            return
        await self.note(f"saved {len(self.history)} message(s) to {path}")
