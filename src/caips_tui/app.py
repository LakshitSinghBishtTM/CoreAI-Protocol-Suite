"""The Textual application: a section sidebar, one pane per API area, and entry points.

Run ``caips-tui`` (or ``caips tui``). Settings: ``CAIPS_URL``; the token is added in the
Keys section (Ctrl+K jumps there) or taken from ``CAIPS_API_KEY``. Nothing is asked at startup.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.command import DiscoveryHit, Hit, Hits, Provider
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import ContentSwitcher, Footer, OptionList, Static
from textual.widgets.option_list import Option

from caips_cli import credentials
from caips_tui import prefs
from caips_tui.chat import ChatPane
from caips_tui.client import ApiError, Client
from caips_tui.harness_view import HarnessPane
from caips_tui.home import HomePane, SettingsPane
from caips_tui.panes import (
    AdminPane,
    AgentsPane,
    ApprovalsPane,
    BudgetsPane,
    EvalsPane,
    FeedbackPane,
    FinetunePane,
    KeysPane,
    LocalPane,
    MemoryPane,
    ModelsPane,
    ProvidersPane,
    RagPane,
    RoutingPane,
    RunsPane,
    StatusPane,
    TasksPane,
    ToolsPane,
    UsagePane,
)
from caips_tui.widgets import Pane

SECTIONS: list[tuple[str, str, type[Pane]]] = [
    ("home", "Home", HomePane),
    ("harness", "Chat", HarnessPane),
    ("chat", "API playground", ChatPane),
    ("keys", "Keys", KeysPane),
    ("providers", "Providers", ProvidersPane),
    ("routing", "Routing", RoutingPane),
    ("models", "Models", ModelsPane),
    ("usage", "Usage", UsagePane),
    ("budgets", "Budgets", BudgetsPane),
    ("status", "Status", StatusPane),
    ("runs", "Runs", RunsPane),
    ("approvals", "Approvals", ApprovalsPane),
    ("tools", "Tools", ToolsPane),
    ("agents", "Agents", AgentsPane),
    ("tasks", "Tasks", TasksPane),
    ("memory", "Memory", MemoryPane),
    ("rag", "RAG", RagPane),
    ("feedback", "Feedback", FeedbackPane),
    ("local", "Local models", LocalPane),
    ("finetune", "Fine-tuning", FinetunePane),
    ("evals", "Evals", EvalsPane),
    ("admin", "Admin", AdminPane),
    ("settings", "Settings", SettingsPane),
]

# How the sidebar groups the sections (every section appears exactly once).
GROUPS: list[tuple[str, list[str]]] = [
    ("START", ["home", "harness", "keys", "settings"]),
    ("GATEWAY", ["providers", "routing", "models", "usage", "budgets", "status"]),
    ("AUTOMATION", ["runs", "approvals", "tools", "agents", "tasks"]),
    ("DATA", ["memory", "rag", "feedback"]),
    ("ADVANCED", ["chat", "local", "finetune", "evals", "admin"]),
]
LABELS = {sid: label for sid, label, _ in SECTIONS}

HELP = """CaiPS terminal UI

Ctrl+B    focus the section list (↑/↓ switch section, Enter moves into it)
Ctrl+K    open Keys: save the gateway token, add provider keys
Ctrl+P    command palette: jump to any section or action
Ctrl+T    toggle light/dark theme
Tab       next control          Shift+Tab   previous control
F5 / Ctrl+R   refresh the current section
Esc       stop a reply          F1   this help      Ctrl+Q   quit

Home     first-run checklist: gateway, token, provider key.
Chat     talk to any model; replies render as Markdown. Defaults are set in Settings.
Settings model, temperature, system prompt, theme, sign out (saved on this computer).
Keys     the only place secrets are entered. Nothing is asked at startup.
API playground (under Advanced) speaks the raw OpenAI / native / Anthropic dialects.

Every button makes one documented API request. Sections that need the admin
scope show the server's error if your token lacks it. Destructive actions ask first.
CAIPS_API_KEY in the environment overrides the saved token; it is never shown."""


THEMES = [
    Theme(
        name="caips-dark",
        primary="#5b9dff",
        secondary="#8b98ad",
        accent="#35c9b0",
        foreground="#d8dee9",
        background="#0b0f14",
        surface="#11161d",
        panel="#19212b",
        success="#3fb97f",
        warning="#e3b341",
        error="#f0605d",
        dark=True,
    ),
    Theme(
        name="caips-light",
        primary="#2457d6",
        secondary="#5b6b86",
        accent="#0f9488",
        foreground="#1b2330",
        background="#f4f6fa",
        surface="#ffffff",
        panel="#e8edf5",
        success="#15803d",
        warning="#b45309",
        error="#b91c1c",
        dark=False,
    ),
]


class SectionCommands(Provider):
    """Command palette (Ctrl+P): jump to any section or run a global action."""

    def _entries(self) -> list[tuple[str, str, Any]]:
        app = self.app
        out: list[tuple[str, str, Any]] = [
            (
                f"Go to {label}",
                f"Open the {label} section",
                (lambda sid=sid: app.goto(sid)),
            )
            for sid, label, _ in SECTIONS
        ]
        out += [
            ("Refresh section", "Reload the current section (F5)", app.action_refresh),
            (
                "Set API token",
                "Open Keys and paste the gateway token (Ctrl+K)",
                app.action_set_key,
            ),
            (
                "Toggle light/dark theme",
                "Switch theme (Ctrl+T)",
                app.action_toggle_theme,
            ),
            ("Show help", "Keys and tips (F1)", app.action_help),
        ]
        return out

    async def discover(self) -> Hits:
        for name, help_, fn in self._entries():
            yield DiscoveryHit(name, fn, help=help_)

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        for name, help_, fn in self._entries():
            score = matcher.match(name)
            if score > 0:
                yield Hit(score, matcher.highlight(name), fn, help=help_)


class HelpScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape,f1,q", "close", "Close")]
    DEFAULT_CSS = """
    HelpScreen { align: center middle; }
    HelpScreen > Static { width: 78; height: auto; padding: 1 2; border: thick $primary; background: $surface; }
    """

    def compose(self) -> ComposeResult:
        yield Static(HELP)

    def action_close(self) -> None:
        self.dismiss(None)


class CaipsTui(App[None]):
    TITLE = "CaiPS"
    HORIZONTAL_BREAKPOINTS = [(0, "-narrow"), (110, "-wide")]
    CSS = """
    Screen { background: $background; }
    Footer { background: $panel; }

    /* top bar */
    #topbar { height: 1; background: $panel; padding: 0 1; }
    #crumb { width: 1fr; }
    #topinfo { width: auto; color: $text-muted; }

    /* sidebar */
    #side { width: 26; background: $surface; border-right: tall $panel; }
    #brand { height: 4; padding: 1 2 0 2; }
    #nav { height: 1fr; background: transparent; border: none; padding: 0; scrollbar-size-vertical: 1; }
    #nav > .option-list--option { padding: 0 2; }
    #nav > .option-list--option-highlighted { background: $primary 25%; color: $foreground; text-style: bold; }
    #nav:focus > .option-list--option-highlighted { background: $primary; color: $background; }
    #conn { height: auto; min-height: 4; padding: 1 2; border-top: tall $panel; background: $panel 60%; }

    /* panes */
    Pane { padding: 1 2 0 2; height: 1fr; }
    .pane-title { text-style: bold; color: $accent; height: 1; }
    .pane-help { color: $text-muted; margin-bottom: 1; }
    .status { height: 1; color: $text-muted; }
    .card { border: round $panel; background: $surface; padding: 0 1; height: auto; margin-bottom: 1; }
    .card-title { color: $accent; text-style: bold; height: 1; }
    #token-state { height: 1; margin-bottom: 1; }

    /* controls */
    Button { border: none; min-width: 8; }
    Button.-default { background: $panel; color: $foreground; }
    Button.-default:hover { background: $primary 40%; }
    Button:hover { text-style: bold; }
    Input:focus { border: none; }
    DataTable { background: $surface; }
    DataTable > .datatable--header { background: $panel; text-style: bold; color: $accent; }
    DataTable > .datatable--cursor { background: $primary 40%; }
    DataTable:focus > .datatable--cursor { background: $primary; color: $background; }

    /* input fields and action buttons wrap into rows instead of overflowing */
    .fields { grid-size: 2; grid-rows: 1; grid-gutter: 1 1; height: auto; margin-bottom: 1; }
    .actions { grid-size: 4; grid-rows: 1; grid-gutter: 0 1; height: auto; margin-bottom: 1; }
    .-wide .fields { grid-size: 3; }
    .-wide .actions { grid-size: 6; }
    .fields Input, .row Input, .frow Input, .fgroup Input { height: 1; border: none; padding: 0 1; background: $panel; }
    .fields Input { width: 100%; }
    .fields Input:focus, .row Input:focus, .frow Input:focus, .fgroup Input:focus { background: $primary 40%; }
    .actions Button, .bar Button, .frow Button.inline { height: 1; min-width: 6; border: none; padding: 0 1; }
    .actions Button { width: 100%; }
    .bar { height: 1; margin: 1 0; }
    .bar Button { margin-right: 1; }
    .row { height: 1; margin-bottom: 1; }
    .row Input { width: 1fr; margin-right: 1; }
    .row Input.wide { width: 3fr; }
    #mode { width: 20; margin-right: 1; }
    #mode > SelectCurrent { border: none; height: 1; padding: 0 1; background: $panel; }

    /* labelled form rows (Harness, Keys) */
    .frow { height: auto; margin-bottom: 1; }
    .fgroup { width: 1fr; height: auto; margin-right: 2; }
    .fgroup.narrow { width: 12; }
    .fgroup.full { width: 100%; margin-right: 0; }
    .frow Button.inline { margin-top: 1; width: auto; }
    #token-card .frow { margin-bottom: 0; }
    #token-card .frow Button.inline { margin-top: 0; }
    #token-card .frow Input { width: 1fr; margin-right: 1; }
    .flabel { color: $text-muted; height: 1; }
    .inline-toggle { width: 100%; }
    .empty-hint { color: $text-muted; width: 100%; margin-top: 2; text-align: center; }
    Collapsible { border-top: none; padding: 0; background: transparent; }
    Collapsible > Contents { padding: 0 0 0 1; height: auto; }
    CollapsibleTitle { padding: 0; color: $accent; text-style: bold; background: transparent; }
    #hx-system { height: 5; border: none; padding: 0 1; background: $panel; }
    #hx-system:focus { background: $primary 25%; }
    #hx-badges { height: 1; margin-bottom: 1; }
    #hx-chips { height: 1; }
    .bar.tools { margin: 1 0 1 0; }
    #cycle-reasoning { min-width: 19; }
    .spacer { width: 1fr; }
    .-narrow .bar.tools .opt { display: none; }
    #prompt { height: 3; border: tall $panel; padding: 0 1; background: $surface; }
    #prompt:focus { border: tall $primary; }
    HarnessPane.incognito #transcript { border: round $warning; }
    HarnessPane.incognito #prompt { border: tall $warning 60%; }
    HarnessPane.incognito #prompt:focus { border: tall $warning; }

    #home-steps { height: auto; margin-bottom: 1; }
    #home-tokenrow { height: auto; }
    #home-tips { margin-top: 1; }
    #settings-scroll { height: 1fr; }
    #set-system { height: 5; border: none; padding: 0 1; background: $panel; }
    #set-system:focus { background: $primary 25%; }
    .fgroup Select { width: 100%; }
    .fgroup Select > SelectCurrent { border: none; height: 1; padding: 0 1; background: $panel; }
    .fgroup Switch { border: none; height: 1; padding: 0; }
    .msg.assistant { padding: 0 1; }

    .body { height: 1fr; }
    .body DataTable { width: 2fr; }
    .detail { width: 1fr; border-left: solid $primary-darken-2; padding: 0 1; }
    .detail.wide { width: 1fr; border-left: none; }
    #transcript { height: 1fr; border: round $panel; padding: 0 1; }
    .msg { margin-bottom: 1; }
    .msg.user { background: $panel; padding: 0 1; }
    .msg.meta { color: $text-muted; text-style: italic; }
    .msg.info { color: $accent; }
    .msg.error { color: $error; }
    """
    BINDINGS = [
        Binding("ctrl+b", "focus_sidebar", "Sections"),
        Binding("ctrl+k", "set_key", "Keys"),
        Binding("f5,ctrl+r", "refresh", "Refresh"),
        Binding("f1", "help", "Help"),
        Binding("ctrl+t", "toggle_theme", "Theme"),
    ]
    COMMANDS = {SectionCommands}

    def __init__(self, client: Client, section: Optional[str] = None):
        super().__init__()
        self.client = client
        self.prefs: dict[str, Any] = prefs.load()
        self.prefs_version = 0
        if section is None:  # first run: guide the user; otherwise straight to chat
            section = "harness" if client.has_key else "home"
        self.start_section = (
            section if any(s[0] == section for s in SECTIONS) else "home"
        )
        self.section = self.start_section

    # -- layout ---------------------------------------------------------
    def nav_options(self) -> list[Option]:
        """Group headers (not selectable) followed by their sections."""
        out: list[Option] = []
        for i, (group, sids) in enumerate(GROUPS):
            head = Text(("" if i == 0 else "\n") + group, style="bold dim")
            out.append(Option(head, id=f"grp-{group}", disabled=True))
            out += [Option(LABELS[sid], id=sid) for sid in sids]
        return out

    def compose(self) -> ComposeResult:
        with Horizontal(id="topbar"):
            yield Static("", id="crumb")
            yield Static("", id="topinfo")
        with Horizontal():
            with Vertical(id="side"):
                yield Static(
                    "[b $accent]◆ CaiPS[/]\n[dim]Protocol Suite[/]", id="brand"
                )
                yield OptionList(*self.nav_options(), id="nav")
                yield Static("", id="conn")
            yield ContentSwitcher(
                *[cls(self.client, id=f"p-{sid}") for sid, _, cls in SECTIONS],
                initial=f"p-{self.start_section}",
                id="panes",
            )
        yield Footer()

    def nav_index(self, sid: str) -> int:
        return self.query_one("#nav", OptionList).get_option_index(sid)

    def set_crumb(self, sid: str) -> None:
        group = next((g for g, sids in GROUPS if sid in sids), "")
        self.query_one("#crumb", Static).update(
            Text.assemble(
                ("◆ CaiPS", "bold"),
                (f"  ›  {group.title()}  ›  ", "dim"),
                (LABELS[sid], "bold"),
            )
        )

    async def on_mount(self) -> None:
        for t in THEMES:
            self.register_theme(t)
        self.theme = (
            self.prefs["theme"]
            if self.prefs["theme"] in {t.name for t in THEMES}
            else "caips-dark"
        )
        self.online: Optional[bool] = None
        self.query_one("#topinfo", Static).update(self.client.base_url)
        self.sub_title = self.client.base_url
        self.set_crumb(self.start_section)
        self.query_one("#nav", OptionList).highlighted = self.nav_index(
            self.start_section
        )
        self.set_interval(15, self.check_connection)
        self.run_worker(self.check_connection(), group="conn", exclusive=True)

    # -- token (entered in the Keys section, never at startup) -----------
    async def action_set_key(self) -> None:
        await self.goto("keys")

    def store_token(self, key: str) -> Optional[str]:
        """Use ``key`` now and save it for this gateway. Returns a warning if it could not be saved."""
        self.client.set_key(key)
        warning: Optional[str] = None
        try:
            credentials.save(self.client.base_url, key)
        except OSError as e:
            warning = f"token kept for this session, not saved: {e.strerror or e}"
        self.run_worker(self.check_connection(), group="conn", exclusive=True)
        return warning

    def forget_token(self) -> None:
        self.client.clear_key()
        try:
            credentials.delete(self.client.base_url)
        except OSError:
            pass
        self.run_worker(self.check_connection(), group="conn", exclusive=True)

    async def on_unmount(self) -> None:
        await self.client.aclose()

    # -- navigation -----------------------------------------------------
    def pane(self, sid: str) -> Pane:
        return self.query_one(f"#p-{sid}", Pane)

    async def show_section(self, sid: str) -> None:
        if sid != self.section:
            self.pane(self.section).deactivate()
        self.section = sid
        self.set_crumb(sid)
        self.query_one("#panes", ContentSwitcher).current = f"p-{sid}"
        self.run_worker(self.pane(sid).activate(), group="activate", exclusive=True)

    async def on_option_list_option_highlighted(
        self, event: OptionList.OptionHighlighted
    ) -> None:
        if event.option_id and not event.option_id.startswith("grp-"):
            await self.show_section(event.option_id)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_id and not event.option_id.startswith("grp-"):
            self.pane(event.option_id).focus_primary()

    def action_focus_sidebar(self) -> None:
        self.query_one("#nav", OptionList).focus()

    async def action_refresh(self) -> None:
        self.run_worker(
            self.pane(self.section).refresh_data(), group="refresh", exclusive=True
        )

    async def goto(self, sid: str) -> None:
        self.query_one("#nav", OptionList).highlighted = self.nav_index(sid)
        await self.show_section(sid)
        self.pane(sid).focus_primary()

    def apply_prefs(self, values: dict[str, Any]) -> None:
        """Use freshly saved settings: theme now, chat defaults when Chat next opens."""
        self.prefs = values
        self.prefs_version += 1
        if values["theme"] in {t.name for t in THEMES}:
            self.theme = values["theme"]

    def action_toggle_theme(self) -> None:
        self.theme = "caips-light" if self.theme == "caips-dark" else "caips-dark"
        self.prefs = {**self.prefs, "theme": self.theme}
        try:
            prefs.save(self.prefs)
        except OSError:
            pass  # the theme still changes for this session

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    # -- connection indicator ------------------------------------------
    async def check_connection(self) -> None:
        conn = self.query_one("#conn", Static)
        if self.client.has_key:
            auth = Text("key set", style="dim")
        else:
            auth = Text.assemble(("no API key", "yellow"), ("\nadd one on Home", "dim"))
        started = time.monotonic()
        try:
            await self.client.request("GET", "/health")
        except ApiError as e:
            conn.update(Text.assemble(("● offline\n", "bold red"), auth))
            conn.tooltip = str(e)
            if self.online is True:
                self.notify("Connection lost", severity="error", title="CaiPS")
            self.online = False
        else:
            ms = (time.monotonic() - started) * 1000
            conn.update(
                Text.assemble(
                    ("● online", "bold green"),
                    (f"  {ms:.0f} ms\n", "dim"),
                    auth,
                )
            )
            conn.tooltip = None
            if self.online is False:
                self.notify("Connection restored", title="CaiPS")
            self.online = True


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="caips-tui",
        description="Terminal UI for a CaiPS gateway. Add the token in the UI's Keys section; it is saved "
        "(see `caips login`) and CAIPS_API_KEY or --api-key-file override it. It is never accepted "
        "on the command line, where other users could see it.",
        allow_abbrev=False,  # so `--api-key SECRET` is an error, not a silent match for --api-key-file
    )
    ap.add_argument("--url", default=os.getenv("CAIPS_URL", "http://localhost:6969"))
    ap.add_argument(
        "--api-key-file",
        help="read the bearer token from this file instead of CAIPS_API_KEY",
    )
    ap.add_argument(
        "--timeout",
        type=float,
        default=60,
        help="seconds to wait for the gateway to respond",
    )
    ap.add_argument(
        "--section",
        default=None,
        choices=[s[0] for s in SECTIONS],
        help="section to open first (default: Home until a token is saved, then Chat)",
    )
    return ap


def main(argv: Optional[list[str]] = None, transport: Any = None) -> int:
    args = build_parser().parse_args(argv)
    key = credentials.resolve(args.url)
    if args.api_key_file:
        try:
            key = Path(args.api_key_file).expanduser().read_text().strip() or None
        except OSError as e:
            print(
                f"caips-tui: cannot read {args.api_key_file}: {e.strerror or e}",
                file=sys.stderr,
            )
            return 2
    client = Client(args.url, key, timeout=args.timeout, transport=transport)
    CaipsTui(client, args.section).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
