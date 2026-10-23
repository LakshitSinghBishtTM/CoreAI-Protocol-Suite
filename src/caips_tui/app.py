"""The Textual application: a section sidebar, one pane per API area, and entry points.

Run ``caips-tui`` (or ``caips tui``). Settings: ``CAIPS_URL``; the token is entered in the UI (Ctrl+K) or taken from ``CAIPS_API_KEY``.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Optional

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import ContentSwitcher, Footer, Header, Input, OptionList, Static
from textual.widgets.option_list import Option

from caips_cli import credentials
from caips_tui.chat import ChatPane
from caips_tui.client import ApiError, Client
from caips_tui.panes import (
    AdminPane,
    AgentsPane,
    ApprovalsPane,
    EvalsPane,
    FeedbackPane,
    FinetunePane,
    LocalPane,
    MemoryPane,
    ModelsPane,
    KeysPane,
    ProvidersPane,
    RagPane,
    RunsPane,
    StatusPane,
    TasksPane,
    ToolsPane,
    UsagePane,
)
from caips_tui.widgets import Pane

SECTIONS: list[tuple[str, str, type[Pane]]] = [
    ("chat", "Chat", ChatPane),
    ("providers", "Providers", ProvidersPane),
    ("keys", "Keys", KeysPane),
    ("models", "Models", ModelsPane),
    ("usage", "Usage", UsagePane),
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
]

HELP = """CaiPS terminal UI

Ctrl+B    focus the section list (↑/↓ switch section, Enter moves into it)
Tab       next control          Shift+Tab   previous control
F5 / Ctrl+R   refresh the current section
Esc       stop a chat reply     F1   this help      Ctrl+Q   quit
Ctrl+K    set API token

Every button makes one documented API request. Sections that need the admin
scope (Stats, Approvals, Local models, Fine-tuning, Admin) show the server's
error if your token lacks it. Destructive actions ask first.

Ctrl+K    set or replace the API token (saved to ~/.config/caips, mode 0600)
CAIPS_API_KEY in the environment overrides the saved token; it is never shown."""


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


class KeyScreen(ModalScreen[Optional[str]]):
    BINDINGS = [Binding("escape", "skip", "Skip")]
    DEFAULT_CSS = """
    KeyScreen { align: center middle; }
    KeyScreen > Vertical { width: 64; height: auto; padding: 1 2; border: thick $primary; background: $surface; }
    """

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("Gateway API token\nEnter to save, Esc to skip. Input is hidden.")
            yield Input(password=True, placeholder="token", id="token")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)

    def action_skip(self) -> None:
        self.dismiss(None)


class CaipsTui(App[None]):
    TITLE = "CaiPS"
    HORIZONTAL_BREAKPOINTS = [(0, "-narrow"), (110, "-wide")]
    CSS = """
    #side { width: 18; border-right: solid $primary-darken-2; }
    #nav { height: 1fr; background: transparent; }
    #conn { height: 2; padding: 0 1; }
    Pane { padding: 0 1; height: 1fr; }
    .pane-title { text-style: bold; color: $accent; }
    .pane-help { color: $text-muted; }

    /* input fields and action buttons wrap into rows instead of overflowing */
    .fields { grid-size: 2; grid-rows: 1; grid-gutter: 0 1; height: auto; margin-bottom: 1; }
    .actions { grid-size: 4; grid-rows: 1; grid-gutter: 0 1; height: auto; margin-bottom: 1; }
    .-wide .fields { grid-size: 3; }
    .-wide .actions { grid-size: 6; }
    .fields Input, .row Input { height: 1; border: none; padding: 0 1; background: $boost; }
    .fields Input { width: 100%; }
    .fields Input:focus, .row Input:focus { background: $primary 40%; }
    .actions Button, .bar Button { height: 1; min-width: 6; border: none; padding: 0 1; }
    .actions Button { width: 100%; }
    .bar { height: 1; margin: 1 0; }
    .bar Button { margin-right: 1; }
    .row { height: 1; margin-bottom: 1; }
    .row Input { width: 1fr; margin-right: 1; }
    .row Input.wide { width: 3fr; }
    #mode { width: 20; margin-right: 1; }
    #mode > SelectCurrent { border: none; height: 1; padding: 0 1; background: $boost; }

    .body { height: 1fr; }
    .body DataTable { width: 2fr; }
    .detail { width: 1fr; border-left: solid $primary-darken-2; padding: 0 1; }
    .detail.wide { width: 1fr; border-left: none; }
    .status { height: 1; }
    #transcript { height: 1fr; border: round $primary-darken-2; padding: 0 1; }
    .msg { margin-bottom: 1; }
    .msg.user { background: $boost; padding: 0 1; }
    .msg.meta { color: $text-muted; text-style: italic; }
    .msg.info { color: $accent; }
    .msg.error { color: $error; }
    """
    BINDINGS = [
        Binding("ctrl+b", "focus_sidebar", "Sections"),
        Binding("ctrl+k", "set_key", "API key"),
        Binding("f5,ctrl+r", "refresh", "Refresh"),
        Binding("f1", "help", "Help"),
    ]

    def __init__(self, client: Client, section: str = "chat"):
        super().__init__()
        self.client = client
        self.start_section = (
            section if any(s[0] == section for s in SECTIONS) else "chat"
        )
        self.section = self.start_section

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal():
            with Vertical(id="side"):
                yield OptionList(
                    *[Option(label, id=sid) for sid, label, _ in SECTIONS], id="nav"
                )
                yield Static("", id="conn")
            yield ContentSwitcher(
                *[cls(self.client, id=f"p-{sid}") for sid, _, cls in SECTIONS],
                initial=f"p-{self.start_section}",
                id="panes",
            )
        yield Footer()

    async def on_mount(self) -> None:
        self.sub_title = self.client.base_url
        nav = self.query_one("#nav", OptionList)
        nav.highlighted = [s[0] for s in SECTIONS].index(self.start_section)
        self.set_interval(15, self.check_connection)
        self.run_worker(self.check_connection(), group="conn", exclusive=True)
        if not self.client.has_key:
            self.action_set_key()

    def action_set_key(self) -> None:
        def done(key: Optional[str]) -> None:
            if not key:
                return
            self.client.set_key(key)
            try:
                credentials.save(self.client.base_url, key)
            except OSError as e:
                self.notify(f"token not saved: {e.strerror or e}", severity="warning")
            self.run_worker(self.check_connection(), group="conn", exclusive=True)
            self.run_worker(self.pane(self.section).refresh_data(), group="refresh", exclusive=True)

        self.push_screen(KeyScreen(), done)

    async def on_unmount(self) -> None:
        await self.client.aclose()

    # -- navigation -----------------------------------------------------
    def pane(self, sid: str) -> Pane:
        return self.query_one(f"#p-{sid}", Pane)

    async def show_section(self, sid: str) -> None:
        if sid != self.section:
            self.pane(self.section).deactivate()
        self.section = sid
        self.query_one("#panes", ContentSwitcher).current = f"p-{sid}"
        self.run_worker(self.pane(sid).activate(), group="activate", exclusive=True)

    async def on_option_list_option_highlighted(
        self, event: OptionList.OptionHighlighted
    ) -> None:
        if event.option_id:
            await self.show_section(event.option_id)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_id:
            self.pane(event.option_id).focus_primary()

    def action_focus_sidebar(self) -> None:
        self.query_one("#nav", OptionList).focus()

    async def action_refresh(self) -> None:
        self.run_worker(
            self.pane(self.section).refresh_data(), group="refresh", exclusive=True
        )

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    # -- connection indicator ------------------------------------------
    async def check_connection(self) -> None:
        conn = self.query_one("#conn", Static)
        auth = "key set" if self.client.has_key else "no API key"
        try:
            await self.client.request("GET", "/health")
        except ApiError as e:
            conn.update(Text.assemble(("● offline\n", "bold red"), (auth, "dim")))
            conn.tooltip = str(e)
        else:
            conn.update(Text.assemble(("● online\n", "bold green"), (auth, "dim")))
            conn.tooltip = None


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="caips-tui",
        description="Terminal UI for a CaiPS gateway. The token is entered inside the UI and saved "
        "(see `caips login`); CAIPS_API_KEY or --api-key-file override it. It is never accepted "
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
        default="chat",
        choices=[s[0] for s in SECTIONS],
        help="section to open first",
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
