"""CaiPS TUI - home-first layout with a harness-style chat.

Run:  python caips_home.py        (or: textual serve caips_home.py --port 6969)
Env:  CAIPS_URL      gateway base URL   (default http://localhost:8743)
      CAIPS_API_KEY  bearer token       (optional; can also be set with ctrl+k)
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    ContentSwitcher,
    DirectoryTree,
    Footer,
    Header,
    Input,
    Label,
    OptionList,
    Select,
    Static,
    Switch,
    TextArea,
)
from textual.widgets.option_list import Option

BASE_URL = os.environ.get("CAIPS_URL", "http://localhost:8743")
MAX_ATTACH_BYTES = 200_000

SECTIONS = [
    ("home", "Home"),
    ("chat", "Chat"),
    ("providers", "Providers"),
    ("models", "Models"),
    ("keys", "Keys"),
    ("usage", "Usage"),
    ("tools", "Tools"),
    ("agents", "Agents"),
    ("tasks", "Tasks"),
    ("memory", "Memory"),
    ("local", "Local models"),
    ("finetune", "Fine-tuning"),
    ("admin", "Admin"),
]
PLACEHOLDERS = {
    "providers": "Provider health, circuit-breaker state, routing policy.\n(GET /v1/providers)",
    "models": "Model registry: context window, capabilities, price.\n(GET /v1/models)",
    "keys": "Provider keys and gateway token.",
    "usage": "Requests, tokens, cost.\n(GET /v1/stats - cost tracking not wired in yet)",
    "tools": "Tools / MCP servers available to chat and agents.",
    "agents": "Agents (experimental).\n(GET /v1/agents)",
    "tasks": "Background tasks.\n(GET /v1/tasks)",
    "memory": "Conversation memory.",
    "local": "Local GGUF models (Ollama / LM Studio).",
    "finetune": "Fine-tuning jobs (OpenAI only).\n(GET /v1/finetune/jobs)",
    "admin": "Shutdown, stats, config.",
}


class FilePicker(ModalScreen[Path | None]):
    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="picker"):
            yield Label("Attach a file (Enter to select, Esc to cancel)")
            yield DirectoryTree(str(Path.cwd()))

    @on(DirectoryTree.FileSelected)
    def _picked(self, e: DirectoryTree.FileSelected) -> None:
        self.dismiss(Path(e.path))


class KeyDialog(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="keybox"):
            yield Label("Gateway API key / bearer token")
            yield Input(
                password=True, id="keyinput", placeholder="paste token, Enter to save"
            )

    @on(Input.Submitted)
    def _save(self, e: Input.Submitted) -> None:
        self.dismiss(e.value.strip() or None)


class Home(VerticalScroll):
    def compose(self) -> ComposeResult:
        yield Static("[b]CoreAI Protocol Suite[/b]", id="home-title")
        yield Static(
            "Multi-provider LLM gateway: routing, failover, caching.", classes="muted"
        )
        yield Static("", id="home-status")
        with Horizontal(id="home-actions"):
            yield Button("New chat", id="go-chat", variant="primary")
            yield Button("Providers", id="go-providers")
            yield Button("Models", id="go-models")
            yield Button("Set API key", id="go-key")

    def set_status(self, text: str) -> None:
        self.query_one("#home-status", Static).update(text)


class SettingsPanel(VerticalScroll):
    """Every model/session knob lives here and only here."""

    def compose(self) -> ComposeResult:
        yield Label("[b]Model settings[/b]")
        yield Label("Provider dialect", classes="muted")
        yield Select(
            [
                ("Auto (routed)", ""),
                ("OpenAI", "openai"),
                ("Anthropic", "anthropic"),
                ("Gemini", "gemini"),
                ("Grok", "grok"),
                ("DeepSeek", "deepseek"),
            ],
            value="",
            allow_blank=False,
            id="s-provider",
        )
        yield Label("Routing policy", classes="muted")
        yield Select(
            [
                (p, p)
                for p in ("balanced", "cheapest", "fastest", "round_robin", "fallback")
            ],
            value="balanced",
            allow_blank=False,
            id="s-routing",
        )
        yield Label("Model (blank = auto)", classes="muted")
        yield Input(id="s-model", placeholder="e.g. gpt-4o")
        yield Label("Temperature", classes="muted")
        yield Input("0.7", id="s-temp")
        yield Label("Max tokens", classes="muted")
        yield Input("1024", id="s-max")
        yield Label("System prompt", classes="muted")
        yield TextArea(id="s-system")
        with Horizontal(classes="switchrow"):
            yield Label("Stream")
            yield Switch(True, id="s-stream")
        with Horizontal(classes="switchrow"):
            yield Label("Server tools")
            yield Switch(False, id="s-tools")

    def values(self) -> dict:
        q = self.query_one

        def num(sel, cast, default):
            try:
                return cast(q(sel, Input).value)
            except ValueError:
                return default

        return {
            "provider": q("#s-provider", Select).value or None,
            "routing": q("#s-routing", Select).value,
            "model": q("#s-model", Input).value.strip() or None,
            "temperature": num("#s-temp", float, 0.7),
            "max_tokens": num("#s-max", int, 1024),
            "system": q("#s-system", TextArea).text.strip(),
            "stream": q("#s-stream", Switch).value,
            "tools": q("#s-tools", Switch).value,
        }


class ChatView(Horizontal):
    def __init__(self, app_ref: "CaiPS") -> None:
        super().__init__(id="chat")
        self.caips = app_ref
        self.history: list[dict] = []
        self.attachments: list[Path] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="chat-main"):
            yield Static("", id="chat-banner")
            yield VerticalScroll(id="log")
            yield Static("", id="chips")
            with Horizontal(id="composer"):
                yield Input(
                    placeholder="Message...  (Enter to send, /help)", id="prompt"
                )
            with Horizontal(id="toolbar"):
                yield Button("+ Attach", id="b-attach")
                yield Button("Tools: off", id="b-tools")
                yield Button("Settings", id="b-settings")
                yield Button("Clear", id="b-clear")
                yield Button("Stop", id="b-stop", variant="warning")
        yield SettingsPanel(id="settings", classes="hidden")

    def on_mount(self) -> None:
        self.refresh_banner()
        self.add_msg(
            "system",
            "Ready. Attach files with [b]+ Attach[/b] or ctrl+o; tune the model under [b]Settings[/b].",
        )

    # ---- helpers
    def refresh_banner(self) -> None:
        b = self.query_one("#chat-banner", Static)
        if self.caips.api_key:
            b.update("")
            b.remove_class("warn")
        else:
            b.update("No API key set - press ctrl+k to add one (needed to send).")
            b.add_class("warn")

    def add_msg(self, role: str, text: str) -> Static:
        w = Static(text, classes=f"msg {role}", markup=(role == "system"))
        if role == "user":
            w.update(f"[b]You[/b]\n{text}")
        elif role == "assistant":
            w.update(text)
        log = self.query_one("#log", VerticalScroll)
        log.mount(w)
        log.scroll_end(animate=False)
        return w

    def render_chips(self) -> None:
        self.query_one("#chips", Static).update(
            "  ".join(f"[reverse] {p.name} [/]" for p in self.attachments)
        )

    async def attach(self) -> None:
        def done(p: Path | None) -> None:
            if p:
                self.attachments.append(p)
                self.render_chips()

        self.caips.push_screen(FilePicker(), done)

    def toggle_settings(self) -> None:
        self.query_one("#settings").toggle_class("hidden")

    # ---- events
    @on(Button.Pressed)
    async def _buttons(self, e: Button.Pressed) -> None:
        bid = e.button.id
        if bid == "b-attach":
            await self.attach()
        elif bid == "b-settings":
            self.toggle_settings()
        elif bid == "b-tools":
            sw = self.query_one("#s-tools", Switch)
            sw.value = not sw.value
        elif bid == "b-clear":
            self.history.clear()
            self.attachments.clear()
            self.render_chips()
            await self.query_one("#log").remove_children()
        elif bid == "b-stop":
            self.caips.workers.cancel_group(self.caips, "stream")

    @on(Switch.Changed, "#s-tools")
    def _tools_sw(self, e: Switch.Changed) -> None:
        self.query_one("#b-tools", Button).label = (
            f"Tools: {'on' if e.value else 'off'}"
        )

    @on(Input.Submitted, "#prompt")
    async def _send(self, e: Input.Submitted) -> None:
        text = e.value.strip()
        if not text:
            return
        e.input.value = ""
        if text == "/help":
            self.add_msg(
                "system",
                "ctrl+o attach · ctrl+s settings · ctrl+k key · ctrl+l clear · esc stop",
            )
            return
        if not self.caips.api_key:
            self.refresh_banner()
            self.add_msg("system", "Set an API key first (ctrl+k).")
            return
        content = text
        for p in self.attachments:
            try:
                data = p.read_bytes()[:MAX_ATTACH_BYTES].decode("utf-8", "replace")
                content += f"\n\n--- file: {p.name} ---\n{data}"
            except OSError as ex:
                self.add_msg("system", f"Could not read {p.name}: {ex}")
        shown = text + ("".join(f"\n[attached {p.name}]" for p in self.attachments))
        self.attachments.clear()
        self.render_chips()
        self.add_msg("user", shown)
        self.history.append({"role": "user", "content": content})
        out = self.add_msg("assistant", "...")
        self.stream_reply(out)

    @work(exclusive=True, group="stream")
    async def stream_reply(self, out: Static) -> None:
        s = self.query_one(SettingsPanel).values()
        msgs = (
            [{"role": "system", "content": s["system"]}] if s["system"] else []
        ) + self.history
        body = {
            "messages": msgs,
            "temperature": s["temperature"],
            "max_tokens": s["max_tokens"],
        }
        for k in ("provider", "model"):
            if s[k]:
                body[k] = s[k]
        headers = {"Authorization": f"Bearer {self.caips.api_key}"}
        path = "/v1/completions/stream" if s["stream"] else "/v1/completions"
        text = ""
        try:
            async with httpx.AsyncClient(base_url=BASE_URL, timeout=120) as c:
                if s["stream"]:
                    async with c.stream("POST", path, json=body, headers=headers) as r:
                        if r.status_code >= 400:
                            raise RuntimeError(f"HTTP {r.status_code}")
                        async for chunk in r.aiter_text():
                            text += chunk
                            out.update(text)
                            self.query_one("#log").scroll_end(animate=False)
                else:
                    r = await c.post(path, json=body, headers=headers)
                    if r.status_code >= 400:
                        raise RuntimeError(f"HTTP {r.status_code}")
                    j = r.json()
                    text = j.get("content") or j.get("text") or json.dumps(j)
                    out.update(text)
            self.history.append({"role": "assistant", "content": text})
        except httpx.ConnectError:
            out.update(f"[red]Cannot reach gateway at {BASE_URL}[/red]")
            out.markup = True
        except Exception as ex:  # noqa: BLE001
            out.update(f"[red]Error: {ex}[/red]")
            out.markup = True


class CaiPS(App):
    TITLE = "CaiPS"
    SUB_TITLE = "Protocol Suite"
    CSS = """
    #nav { width: 22; border-right: solid $primary-darken-2; }
    #content { padding: 0 1; }
    .hidden { display: none; }
    .muted { color: $text-muted; }
    #home-title { padding: 1 0 0 0; text-style: bold; }
    #home-status { margin: 1 0; }
    #home-actions { height: 3; }
    #home-actions Button { margin-right: 1; }
    ChatView { height: 1fr; }
    #chat-main { width: 1fr; }
    #settings { width: 36; border-left: solid $primary-darken-2; padding: 0 1; }
    #log { height: 1fr; border: round $primary-darken-2; padding: 0 1; }
    .msg { margin: 0 0 1 0; }
    .user { color: $accent; }
    .system { color: $text-muted; }
    #chat-banner.warn { background: $warning 30%; padding: 0 1; }
    #chat-banner { height: auto; }
    #chips { height: auto; }
    #composer { height: 3; }
    #prompt { width: 1fr; }
    #toolbar { height: 3; }
    #toolbar Button { margin-right: 1; min-width: 10; }
    .switchrow { height: 3; }
    .switchrow Label { width: 1fr; padding-top: 1; }
    #s-system { height: 6; }
    #picker, #keybox { width: 70; height: 24; background: $surface; border: thick $primary; padding: 1; }
    #keybox { height: 8; }
    FilePicker, KeyDialog { align: center middle; }
    """
    BINDINGS = [
        Binding("ctrl+k", "key", "API key"),
        Binding("ctrl+o", "attach", "Attach"),
        Binding("ctrl+s", "settings", "Settings"),
        Binding("ctrl+l", "clear", "Clear"),
        Binding("f1", "section('home')", "Home"),
        Binding("escape", "stop", "Stop", show=False),
        Binding("ctrl+q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.api_key = os.environ.get("CAIPS_API_KEY", "")

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield OptionList(
                *[Option(label, id=sid) for sid, label in SECTIONS], id="nav"
            )
            with ContentSwitcher(initial="home", id="content"):
                yield Home(id="home")
                yield ChatView(self)
                for sid, _ in SECTIONS[2:]:
                    yield Static(
                        f"[b]{dict(SECTIONS)[sid]}[/b]\n\n{PLACEHOLDERS[sid]}", id=sid
                    )
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#nav", OptionList).highlighted = 0
        self.check_gateway()

    @work(exclusive=True, group="health")
    async def check_gateway(self) -> None:
        home = self.query_one(Home)
        try:
            async with httpx.AsyncClient(base_url=BASE_URL, timeout=3) as c:
                j = (await c.get("/health")).json()
            provs = j.get("providers") or []
            home.set_status(
                f"[green]online[/green]  {BASE_URL}\nProviders: {', '.join(map(str, provs)) or 'none'}"
                f"\nAPI key: {'set' if self.api_key else 'not set (only needed to chat)'}"
            )
        except Exception:  # noqa: BLE001
            home.set_status(
                f"[red]offline[/red]  {BASE_URL}\nStart the gateway: uvicorn api.server:app --port 8743"
            )

    def show(self, sid: str) -> None:
        self.query_one("#content", ContentSwitcher).current = sid
        nav = self.query_one("#nav", OptionList)
        nav.highlighted = [s for s, _ in SECTIONS].index(sid)
        if sid == "chat":
            self.query_one("#prompt", Input).focus()

    @on(OptionList.OptionHighlighted, "#nav")
    @on(OptionList.OptionSelected, "#nav")
    def _nav(self, e) -> None:
        if e.option.id:
            self.query_one("#content", ContentSwitcher).current = e.option.id

    @on(Button.Pressed, "#go-chat")
    def _go_chat(self) -> None:
        self.show("chat")

    @on(Button.Pressed, "#go-providers")
    def _go_prov(self) -> None:
        self.show("providers")

    @on(Button.Pressed, "#go-models")
    def _go_models(self) -> None:
        self.show("models")

    @on(Button.Pressed, "#go-key")
    def _go_key(self) -> None:
        self.action_key()

    # actions
    def action_section(self, sid: str) -> None:
        self.show(sid)

    def action_key(self) -> None:
        def done(k: str | None) -> None:
            if k:
                self.api_key = k
                self.query_one(ChatView).refresh_banner()
                self.check_gateway()

        self.push_screen(KeyDialog(), done)

    async def action_attach(self) -> None:
        self.show("chat")
        await self.query_one(ChatView).attach()

    def action_settings(self) -> None:
        self.show("chat")
        self.query_one(ChatView).toggle_settings()

    def action_clear(self) -> None:
        self.query_one(ChatView).query_one("#b-clear", Button).press()

    def action_stop(self) -> None:
        self.workers.cancel_group(self, "stream")


if __name__ == "__main__":
    CaiPS().run()
