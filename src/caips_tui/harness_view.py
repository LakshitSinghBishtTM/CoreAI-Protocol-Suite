"""Harness: a chat session with its whole configuration in view.

Top: model, provider, policy, sampling, history window, incognito, system prompt
and context files. Bottom: the transcript, then a tool strip (server tools,
reasoning, streaming) and the prompt box. It reuses the OpenAI-chat request
path of :class:`ChatPane`, so it makes the same documented calls.

What each control does (nothing here needs a gateway feature that does not exist):

* **Tools** sets ``run_tools`` (the gateway runs the tools this token is granted).
* **Reasoning** adds a "think step by step" instruction to the system prompt
  (``brief`` or ``deep``). The gateway has no reasoning parameter, so this is
  prompt-level and works with any model.
* **Incognito** (a toggle) sends ``metadata.private = true`` (local servers only, see
  docs/local-models.md) and switches off feedback and ``/save`` for the session.
* **Context** files are read here and sent in the system prompt on every turn.
* **History turns** keeps only the last N exchanges in each request.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Optional

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Collapsible, Input, Label, Static, TextArea

from caips_tui.chat import ChatPane

MAX_FILE_BYTES = 200_000
MAX_FILES = 5

REASONING = {
    "off": "",
    "brief": "Think through the problem step by step before answering, and keep the reasoning short.",
    "deep": (
        "Reason carefully and thoroughly: break the problem into steps, consider alternatives, "
        "check your work, then give the final answer clearly at the end."
    ),
}
REASONING_ORDER = ["off", "brief", "deep"]

HELP_TEXT = """Harness commands
  /clear              start a new conversation (context files stay)
  /retry              ask the last question again
  /explain [text]     dry-run routing (nothing is sent to a provider)
  /attach <path>      add a context file          /detach   drop all context files
  /incognito on|off   local-only routing, no feedback or saving
  /reasoning off|brief|deep      /tools on|off      /stream on|off
  /system <text>  /model <name>  /provider <name>  /policy <name>
  /good [comment]  /bad [correction]  /save <path>
Esc stops a reply in progress."""


def field(label: str, widget: Any, classes: str = "") -> Vertical:
    """A labelled form control: small caption above the input."""
    return Vertical(
        Label(label, classes="flabel"), widget, classes=f"fgroup {classes}".strip()
    )


class HarnessPane(ChatPane):
    TITLE = "Harness"
    HELP = ""
    FLAGS = {"tools": "Tools", "stream": "Stream"}

    def __init__(self, *a: Any, **kw: Any):
        super().__init__(*a, **kw)
        self.reasoning = "off"
        self.incognito = False
        self.files: dict[str, str] = {}

    # -- layout ---------------------------------------------------------
    def body(self) -> ComposeResult:
        yield Static("", id="hx-badges")
        with Collapsible(title="Session settings", collapsed=False, id="hx-config"):
            with Horizontal(classes="frow"):
                yield field(
                    "Model", Input(placeholder="auto: router picks", id="fld-model")
                )
                yield field("Provider", Input(placeholder="auto", id="fld-provider"))
                yield field(
                    "Routing policy", Input(placeholder="default", id="fld-policy")
                )
            with Horizontal(classes="frow"):
                yield field("Temperature", Input(value="0.7", id="fld-temperature"))
                yield field("Max tokens", Input(value="1024", id="fld-max_tokens"))
                yield field("History turns", Input(placeholder="all", id="fld-turns"))
                yield field(
                    "Incognito",
                    Button(
                        "off",
                        id="btn-incognito",
                        classes="inline-toggle",
                        tooltip="Route to local servers only; feedback and saving are switched off",
                    ),
                    "narrow",
                )
            yield field(
                "System prompt",
                TextArea(id="hx-system", show_line_numbers=False, soft_wrap=True),
                "full",
            )
            with Horizontal(classes="frow"):
                yield field(
                    "Context files",
                    Input(
                        placeholder="path to a file, Enter to attach", id="fld-attach"
                    ),
                    "grow",
                )
                yield Button("Clear context", id="ctx-clear", classes="inline")
            yield Static("", id="hx-chips")
        yield VerticalScroll(id="transcript")
        with Horizontal(classes="bar tools"):
            for name in self.FLAGS:
                yield Button(
                    self.flag_label(name),
                    id=f"tg-{name}",
                    variant="success" if self.flags[name] else "default",
                )
            yield Button(self.reasoning_label(), id="cycle-reasoning")
            yield Static("", classes="spacer")
            # on narrow terminals only Clear and Stop stay; the rest are slash commands
            yield Button("Explain", id="explain", classes="opt")
            yield Button("Good", id="good", variant="success", classes="opt")
            yield Button("Bad", id="bad", variant="error", classes="opt")
            yield Button("Clear", id="clear")
            yield Button("Stop", id="stop", variant="warning")
        yield Input(
            placeholder="Message the harness …  (/help for commands)", id="prompt"
        )

    def on_mount(self) -> None:
        super().on_mount()
        self.update_badges()
        self.run_worker(self.show_empty_hint(), group="hint")
        try:
            if (
                self.app.size.height < 40
            ):  # leave room for the transcript on small terminals
                self.query_one("#hx-config", Collapsible).collapsed = True
        except Exception:
            pass

    # -- empty state ---------------------------------------------------------
    async def show_empty_hint(self) -> None:
        if self.history or self.query(".empty-hint"):
            return
        lines = [
            "Start a conversation below.",
            "",
            "/help lists commands · Esc stops a reply",
        ]
        if not self.client.has_key:
            lines[1:1] = [
                "No gateway token yet: press Ctrl+K to open Keys and add one."
            ]
        hint = Static(Text("\n".join(lines), justify="center"), classes="empty-hint")
        await self.query_one("#transcript", VerticalScroll).mount(hint)

    # -- state shown to the user ------------------------------------------
    def reasoning_label(self) -> str:
        return f"Reasoning: {self.reasoning}"

    def estimate_tokens(self) -> int:
        chars = len(self.val("system")) + sum(len(t) for t in self.files.values())
        chars += sum(len(str(m.get("content", ""))) for m in self.history)
        return chars // 4

    def update_badges(self) -> None:
        t = Text()
        if self.incognito:
            t.append(" INCOGNITO ", style="bold black on yellow")
            t.append("  local servers only", style="yellow")
        else:
            t.append("● standard session", style="dim")
        t.append(
            f"    ctx ≈{self.estimate_tokens():,} tok · {len(self.history)} msg · {len(self.files)} file(s)",
            style="dim",
        )
        try:
            self.query_one("#hx-badges", Static).update(t)
            chips = self.query_one("#hx-chips", Static)
            chips.update(
                Text("  ").join(Text(f" {n} ", style="reverse") for n in self.files)
                if self.files
                else Text("no context files", style="dim")
            )
            self.set_class(self.incognito, "incognito")
            tog = self.query_one("#btn-incognito", Button)
            tog.label = "ON" if self.incognito else "off"
            tog.variant = "warning" if self.incognito else "default"
            for bid in ("good", "bad"):
                self.query_one(f"#{bid}", Button).disabled = self.incognito
        except Exception:  # not mounted yet
            pass

    # -- field overrides (system prompt is a TextArea here) -----------------
    def val(self, field_id: str) -> str:
        if field_id == "system":
            try:
                return self.query_one("#hx-system", TextArea).text.strip()
            except Exception:
                return ""
        return super().val(field_id)

    def set_val(self, field_id: str, value: str) -> None:
        if field_id == "system":
            try:
                self.query_one("#hx-system", TextArea).text = value
            except Exception:
                pass
            return
        super().set_val(field_id, value)

    # -- request shaping --------------------------------------------------
    def context_text(self) -> str:
        if not self.files:
            return ""
        parts = ["Reference files supplied by the user:"]
        parts += [f"--- {name} ---\n{text}" for name, text in self.files.items()]
        return "\n\n".join(parts)

    def turns(self) -> Optional[int]:
        raw = self.val("turns")
        if not raw:
            return None
        try:
            n = int(raw)
        except ValueError:
            return -1
        return n if n > 0 else -1

    def settings(self) -> Optional[dict]:
        s = super().settings()
        if s is None:
            return None
        if self.turns() == -1:
            self.say(
                "History turns must be a whole number above 0 (or blank for all).",
                error=True,
            )
            return None
        parts = [
            p
            for p in (s["system"], REASONING[self.reasoning], self.context_text())
            if p
        ]
        s["system"] = "\n\n".join(parts) or None
        return s

    def window(self) -> list[dict]:
        msgs = list(self.history)
        n = self.turns()
        if n and n > 0:
            keep = 2 * n + (1 if msgs and msgs[-1]["role"] == "user" else 0)
            msgs = msgs[-keep:]
            while msgs and msgs[0]["role"] != "user":
                msgs.pop(0)
        return msgs

    def extra_body(self) -> dict:
        return {"metadata": {"private": True}} if self.incognito else {}

    # -- transcript: label who is speaking ---------------------------------
    async def add(self, kind: str, text: str) -> Static:
        for hint in self.query(".empty-hint"):
            await hint.remove()
        if kind in ("user", "assistant"):
            label = "You" if kind == "user" else "Assistant"
            body = Text.assemble((f"{label}\n", "bold"), text)
            w = Static(body, classes=f"msg {kind}")
            t = self.query_one("#transcript", VerticalScroll)
            await t.mount(w)
            t.scroll_end(animate=False)
            return w
        return await super().add(kind, text)

    async def send(self, text: str) -> None:
        await super().send(text)
        self.update_badges()

    async def clear(self) -> None:
        await super().clear()
        self.update_badges()
        await self.show_empty_hint()

    # -- controls -----------------------------------------------------------
    def set_incognito(self, on: bool) -> None:
        self.incognito = on
        self.update_badges()

    def set_reasoning(self, level: str) -> None:
        self.reasoning = level
        try:
            btn = self.query_one("#cycle-reasoning", Button)
            btn.label = self.reasoning_label()
            btn.variant = "success" if level != "off" else "default"
        except Exception:
            pass

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "btn-incognito":
            event.stop()
            self.set_incognito(not self.incognito)
        elif bid == "cycle-reasoning":
            event.stop()
            nxt = REASONING_ORDER[(REASONING_ORDER.index(self.reasoning) + 1) % 3]
            self.set_reasoning(nxt)
        elif bid == "ctx-clear":
            event.stop()
            self.files.clear()
            self.update_badges()
        else:
            await super().on_button_pressed(event)

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "fld-attach":
            event.stop()
            path = event.value.strip()
            event.input.value = ""
            if path:
                await self.attach(path)
            return
        await super().on_input_submitted(event)

    async def attach(self, path: str) -> None:
        p = Path(path).expanduser()
        if len(self.files) >= MAX_FILES and p.name not in self.files:
            self.say(
                f"At most {MAX_FILES} context files; clear some first.", error=True
            )
            return
        try:
            raw = await asyncio.to_thread(lambda: p.read_bytes()[: MAX_FILE_BYTES + 1])
        except OSError as e:
            self.say(f"cannot read {path}: {e.strerror or e}", error=True)
            return
        clipped = len(raw) > MAX_FILE_BYTES
        self.files[p.name] = raw[:MAX_FILE_BYTES].decode("utf-8", "replace")
        self.update_badges()
        self.say(
            f"attached {p.name}"
            + (f" (first {MAX_FILE_BYTES:,} bytes only)" if clipped else "")
        )

    # -- slash commands -----------------------------------------------------
    async def command(self, line: str) -> None:
        cmd, _, arg = line[1:].partition(" ")
        arg = arg.strip()
        if cmd == "help":
            await self.note(HELP_TEXT)
        elif cmd == "attach":
            (
                await self.attach(arg)
                if arg
                else await self.note("Usage: /attach <path>", error=True)
            )
        elif cmd == "detach":
            self.files.clear()
            self.update_badges()
            await self.note("context files removed")
        elif cmd == "incognito" and arg in ("on", "off"):
            self.set_incognito(arg == "on")
            await self.note(f"incognito = {arg}")
        elif cmd == "reasoning" and arg in REASONING:
            self.set_reasoning(arg)
            await self.note(f"reasoning = {arg}")
        elif cmd == "tools" and arg in ("on", "off"):
            self.set_flag("tools", arg == "on")
            await self.note(f"tools = {arg}")
        else:
            await super().command(line)

    # -- no traces in incognito ---------------------------------------------
    async def feedback(self, rating: int, text: str) -> None:
        if self.incognito:
            await self.note("Incognito: feedback is off for this session.", error=True)
            return
        await super().feedback(rating, text)

    async def save(self, path: str) -> None:
        if self.incognito:
            await self.note("Incognito: saving is off for this session.", error=True)
            return
        await super().save(path)
