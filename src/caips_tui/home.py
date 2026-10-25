"""Home (first-run checklist) and Settings (saved chat defaults, theme, sign-out).

Home answers "what do I do now?": it checks the gateway, the token and the provider
keys, shows what is missing, and offers the one button that fixes it. Settings keeps
every default in one place instead of in the chat screen; the chat applies them when
it opens (and "Session settings" there still overrides them for one conversation).
"""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, Input, Select, Static, Switch, TextArea

from caips_tui import prefs
from caips_tui.client import ApiError
from caips_tui.harness_view import field
from caips_tui.widgets import Pane

TIPS = (
    "Ctrl+P  jump anywhere      Ctrl+K  keys      F1  help\n"
    "Inside Chat: /help lists commands, Esc stops a reply."
)


def step_line(done: bool | None, title: str, detail: str) -> Text:
    mark, style = {True: ("✔", "bold green"), False: ("○", "bold yellow"), None: ("·", "dim")}[
        done
    ]
    t = Text()
    t.append(f" {mark} ", style=style)
    t.append(f"{title:<10}", style="bold")
    t.append(f"{detail}\n", style="dim")
    return t


class HomePane(Pane):
    TITLE = "Welcome to CaiPS"
    HELP = "One gateway for every model. Finish the steps below, then start chatting."

    def body(self) -> ComposeResult:
        yield Static("", id="home-steps")
        with Horizontal(classes="frow", id="home-tokenrow"):
            yield field(
                "Gateway token",
                Input(
                    placeholder="paste it here, Enter to save",
                    password=True,
                    id="home-token",
                ),
                "grow",
            )
            yield Button("Save token", id="home-save", classes="inline")
        with Horizontal(classes="bar"):
            yield Button("Start chatting", id="home-chat", variant="primary")
            yield Button("Add provider key", id="home-keys")
            yield Button("Settings", id="home-settings")
        yield Static(TIPS, id="home-tips", classes="pane-help")

    def focus_primary(self) -> None:
        row = self.query_one("#home-tokenrow")
        target = "#home-token" if row.display else "#home-chat"
        self.query_one(target).focus()

    async def refresh_data(self) -> None:
        online = True
        try:
            await self.client.request("GET", "/health")
        except ApiError:
            online = False
        has_key = self.client.has_key
        providers: list[dict] = []
        provider_note = "needs a working token first"
        if online and has_key:
            try:
                data = await self.client.request("GET", "/v1/providers")
                providers = [p for p in (data or []) if p.get("available")]
                provider_note = (
                    ", ".join(p["name"] for p in providers)
                    if providers
                    else "none yet: add one under Keys"
                )
            except ApiError as e:
                provider_note = f"could not check ({e.detail})"
        out = Text()
        out.append_text(
            step_line(
                online,
                "Gateway",
                self.client.base_url if online else f"cannot reach {self.client.base_url}",
            )
        )
        out.append_text(
            step_line(
                has_key,
                "Token",
                "saved" if has_key else "paste your gateway token below",
            )
        )
        out.append_text(
            step_line(
                bool(providers) if (online and has_key) else None,
                "Provider",
                provider_note,
            )
        )
        if not online:
            out.append(
                "\n Start it with:  uvicorn api.server:app --port 6969\n", style="yellow"
            )
        self.query_one("#home-steps", Static).update(out)
        self.query_one("#home-tokenrow").display = not has_key
        ready = online and has_key and bool(providers)
        self.query_one("#home-chat", Button).variant = "success" if ready else "primary"
        self.say("Ready. Press Start chatting." if ready else "")

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "home-token":
            event.stop()
            await self.save_token()

    async def save_token(self) -> None:
        box = self.query_one("#home-token", Input)
        value = box.value.strip()
        box.value = ""
        if not value:
            self.say("Paste a token first.", error=True)
            return
        warning: Any = self.app.store_token(value)  # type: ignore[attr-defined]
        await self.refresh_data()
        if warning:
            self.say(warning, error=True)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        event.stop()
        if bid == "home-save":
            await self.save_token()
        elif bid == "home-chat":
            await self.app.goto("harness")  # type: ignore[attr-defined]
        elif bid == "home-keys":
            await self.app.goto("keys")  # type: ignore[attr-defined]
        elif bid == "home-settings":
            await self.app.goto("settings")  # type: ignore[attr-defined]


class SettingsPane(Pane):
    TITLE = "Settings"
    HELP = "Defaults for every new chat. Saved on this computer; no secrets are stored here."

    def body(self) -> ComposeResult:
        with VerticalScroll(id="settings-scroll"):
            with Horizontal(classes="frow"):
                yield field("Model", Input(placeholder="auto: router picks", id="set-model"))
                yield field("Provider", Input(placeholder="auto", id="set-provider"))
                yield field("Routing policy", Input(placeholder="default", id="set-policy"))
            with Horizontal(classes="frow"):
                yield field("Temperature", Input(id="set-temperature"))
                yield field("Max tokens", Input(id="set-max_tokens"))
                yield field("History turns", Input(placeholder="all", id="set-turns"))
                yield field(
                    "Reasoning",
                    Select(
                        [("Off", "off"), ("Brief", "brief"), ("Deep", "deep")],
                        value="off",
                        allow_blank=False,
                        id="set-reasoning",
                    ),
                )
            yield field(
                "System prompt",
                TextArea(id="set-system", show_line_numbers=False, soft_wrap=True),
                "full",
            )
            with Horizontal(classes="frow"):
                yield field("Server tools", Switch(False, id="set-tools"))
                yield field("Stream replies", Switch(True, id="set-stream"))
                yield field(
                    "Theme",
                    Select(
                        [("Dark", "caips-dark"), ("Light", "caips-light")],
                        value="caips-dark",
                        allow_blank=False,
                        id="set-theme",
                    ),
                )
            with Horizontal(classes="bar"):
                yield Button("Save", id="set-save", variant="primary")
                yield Button("Reset to defaults", id="set-reset")
                yield Button("Sign out", id="set-signout", variant="error")

    def focus_primary(self) -> None:
        self.query_one("#set-model", Input).focus()

    async def activate(self) -> None:
        self.load_into_form(self.app.prefs)  # type: ignore[attr-defined]
        await super().activate()

    def load_into_form(self, p: dict) -> None:
        q = self.query_one
        for k in ("model", "provider", "policy", "temperature", "max_tokens", "turns"):
            q(f"#set-{k}", Input).value = str(p[k])
        q("#set-system", TextArea).text = p["system"]
        q("#set-reasoning", Select).value = p["reasoning"]
        q("#set-theme", Select).value = p["theme"]
        q("#set-tools", Switch).value = bool(p["tools"])
        q("#set-stream", Switch).value = bool(p["stream"])

    def read_form(self) -> dict | None:
        q = self.query_one
        out: dict[str, Any] = {
            k: q(f"#set-{k}", Input).value.strip()
            for k in ("model", "provider", "policy", "temperature", "max_tokens", "turns")
        }
        for k, kind, what in (
            ("temperature", float, "Temperature must be a number"),
            ("max_tokens", int, "Max tokens must be a whole number"),
            ("turns", int, "History turns must be a whole number"),
        ):
            if out[k]:
                try:
                    if kind(out[k]) < 0 or (kind is int and kind(out[k]) == 0):
                        raise ValueError
                except ValueError:
                    self.say(what + " above 0 (or blank).", error=True)
                    return None
        out["system"] = q("#set-system", TextArea).text.strip()
        out["reasoning"] = str(q("#set-reasoning", Select).value)
        out["theme"] = str(q("#set-theme", Select).value)
        out["tools"] = q("#set-tools", Switch).value
        out["stream"] = q("#set-stream", Switch).value
        return out

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        event.stop()
        app: Any = self.app
        if bid == "set-save":
            values = self.read_form()
            if values is None:
                return
            try:
                prefs.save(values)
            except OSError as e:
                self.say(f"could not save: {e.strerror or e}", error=True)
                return
            app.apply_prefs(values)
            self.say("Saved. New chats use these defaults.")
        elif bid == "set-reset":
            self.load_into_form(dict(prefs.DEFAULTS))
            self.say("Defaults restored in the form; press Save to keep them.")
        elif bid == "set-signout":
            app.forget_token()
            self.say("Signed out: the saved token was removed.")
