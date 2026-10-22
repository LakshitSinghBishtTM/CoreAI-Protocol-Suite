"""Shared building blocks: a confirmation modal, a base pane, and a table pane.

Most sections of the TUI are a ``TablePane``: a list endpoint rendered as a
table, the selected row's full JSON beside it, optional input fields, and a row
of buttons that each make one API call.
"""

from __future__ import annotations

import json
from typing import Any, Callable, NamedTuple, Optional

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical
from textual.coordinate import Coordinate
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Label, RichLog, Static
from textual.worker import WorkerCancelled

from caips_tui.client import ApiError, Client, pretty


class Confirm(ModalScreen[bool]):
    """Yes/No prompt for anything destructive or costly."""

    BINDINGS = [
        Binding("escape", "no", "Cancel"),
        Binding("y", "yes", "Yes"),
        Binding("n", "no", "No"),
    ]
    DEFAULT_CSS = """
    Confirm { align: center middle; }
    Confirm #confirm-box { width: 64; height: auto; padding: 1 2; border: thick $error; background: $surface; }
    Confirm #confirm-buttons { height: auto; margin-top: 1; align-horizontal: right; }
    Confirm Button { margin-left: 2; }
    """

    def __init__(self, text: str):
        super().__init__()
        self.text = text

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Label(self.text)
            with Horizontal(id="confirm-buttons"):
                yield Button("Yes", id="yes", variant="error")
                yield Button("No", id="no", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#no", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


def dig(row: Any, dotted: str, default: Any = None) -> Any:
    """``dig(row, "a.b")`` is ``row["a"]["b"]``, or ``default`` if any step is missing."""
    cur = row
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return default
    return cur


def cell(value: Any, width: int = 60) -> str:
    """One table cell: short, single-line, never raises."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.6g}"
    if isinstance(value, (dict, list)):
        value = json.dumps(value, default=str, ensure_ascii=False)
    s = str(value).replace("\n", " ")
    return s if len(s) <= width else s[: width - 1] + "…"


class Field(NamedTuple):
    id: str
    placeholder: str
    value: str = ""
    password: bool = False


class Action(NamedTuple):
    id: str
    label: str
    needs_row: bool = False
    confirm: str = (
        ""  # non-empty: ask first; may use {column} placeholders from the row
    )
    variant: str = "default"


class _Blank(dict):
    def __missing__(self, key: str) -> str:
        return "?"


class Pane(Vertical):
    """A section of the UI. Subclasses override ``body`` and ``refresh_data``."""

    TITLE = ""
    HELP = ""
    REFRESH = 0.0  # seconds between automatic refreshes while visible; 0 = off

    def __init__(self, client: Client, **kw: Any):
        super().__init__(**kw)
        self.client = client
        self.is_current = False

    # -- layout ---------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Static(self.TITLE, classes="pane-title")
        if self.HELP:
            yield Static(self.HELP, classes="pane-help")
        yield from self.body()
        yield Static("", classes="status")

    def body(self) -> ComposeResult:
        return iter(())

    # -- lifecycle ------------------------------------------------------
    def on_mount(self) -> None:
        if self.REFRESH:
            self.set_interval(self.REFRESH, self._tick)

    async def _tick(self) -> None:
        if self.is_current:
            await self.refresh_data()

    async def activate(self) -> None:
        self.is_current = True
        await self.refresh_data()

    def deactivate(self) -> None:
        self.is_current = False

    async def refresh_data(self) -> None:
        """Reload whatever the pane shows. Default: nothing to load."""

    def focus_primary(self) -> None:
        """Move focus into the pane (Enter in the sidebar does this)."""
        for selector in ("Input", "DataTable", "Button"):
            found = self.query(selector)
            if found:
                found.first().focus()
                return

    # -- helpers --------------------------------------------------------
    def say(self, msg: str, error: bool = False) -> None:
        try:
            self.query_one(".status", Static).update(
                Text(msg, style="bold red" if error else "green")
            )
        except Exception:  # pane not mounted (yet or any more)
            pass

    async def api(self, method: str, path: str, **kw: Any) -> tuple[bool, Any]:
        """Make a request; on failure show the error in the status line."""
        try:
            return True, await self.client.request(method, path, **kw)
        except ApiError as e:
            self.say(str(e), error=True)
            return False, e.detail

    def val(self, field_id: str) -> str:
        try:
            return self.query_one(f"#fld-{field_id}", Input).value.strip()
        except Exception:
            return ""

    def set_val(self, field_id: str, value: str) -> None:
        try:
            self.query_one(f"#fld-{field_id}", Input).value = value
        except Exception:
            pass

    def inputs(self, fields: list[Field]) -> ComposeResult:
        with Grid(classes="fields"):
            for f in fields:
                yield Input(
                    placeholder=f.placeholder,
                    value=f.value,
                    password=f.password,
                    id=f"fld-{f.id}",
                )

    def detail_log(self) -> RichLog:
        return self.query_one(".detail", RichLog)

    def render_detail(self, value: Any) -> None:
        log = self.detail_log()
        log.clear()
        if value is not None:
            log.write(Text(pretty(value)))


class TablePane(Pane):
    """List endpoint -> table + detail, with inputs and action buttons."""

    PATH = ""
    COLUMNS: Optional[list[tuple[str, str]]] = (
        None  # (header, dotted key); None = infer
    )
    ID_KEY = "id"
    FIELDS: list[Field] = []
    ACTIONS: list[Action] = []

    def __init__(self, client: Client, **kw: Any):
        super().__init__(client, **kw)
        self.rows: dict[str, dict] = {}
        self.quiet_next = (
            False  # keep an action's status message through the refresh after it
        )

    # -- what to fetch --------------------------------------------------
    def list_path(self) -> str:
        return self.PATH

    def params(self) -> dict:
        return {}

    def unwrap(self, data: Any) -> list[dict]:
        if isinstance(data, dict):
            for k in ("data", "rows", "events", "tasks"):
                if isinstance(data.get(k), list):
                    return data[k]
            return [data]
        return (
            [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []
        )

    async def fetch(self) -> Optional[list[dict]]:
        """Rows to show, or ``None`` after an error (already reported)."""
        ok, data = await self.api("GET", self.list_path(), params=self.params())
        return self.unwrap(data) if ok else None

    def row_id(self, row: dict, i: int) -> str:
        return str(dig(row, self.ID_KEY, i))

    # -- layout ---------------------------------------------------------
    def body(self) -> ComposeResult:
        if self.FIELDS:
            yield from self.inputs(self.FIELDS)
        with Grid(classes="actions"):
            yield Button("Refresh", id="refresh", variant="primary")
            for a in self.ACTIONS:
                yield Button(a.label, id=f"act-{a.id}", variant=a.variant)  # type: ignore[arg-type]
        with Horizontal(classes="body"):
            yield DataTable(cursor_type="row", zebra_stripes=True)
            yield RichLog(classes="detail", wrap=True, markup=False, highlight=False)

    # -- data -----------------------------------------------------------
    async def refresh_data(self) -> None:
        rows = await self.fetch()
        if rows is None:
            return
        self.fill_table(rows)
        if self.quiet_next:
            self.quiet_next = False
        else:
            self.say(self.status_line(rows))

    def status_line(self, rows: list[dict]) -> str:
        return f"{len(rows)} row(s)"

    def columns_for(self, rows: list[dict]) -> list[tuple[str, str]]:
        if self.COLUMNS:
            return self.COLUMNS
        keys: list[str] = []
        for row in rows[:20]:
            for k, v in row.items():
                if k not in keys and not isinstance(v, (dict, list)):
                    keys.append(k)
        return [(k, k) for k in keys[:8]]

    def fill_table(self, rows: list[dict]) -> None:
        table = self.query_one(DataTable)
        keep = self.selected_key()
        table.clear(columns=True)
        cols = self.columns_for(rows)
        if not cols:
            self.rows = {}
            self.render_detail(None)
            return
        table.add_columns(*[h for h, _ in cols])
        self.rows = {}
        for i, row in enumerate(rows):
            key = self.row_id(row, i)
            if key in self.rows:
                key = f"{key}#{i}"
            self.rows[key] = row
            table.add_row(*[cell(dig(row, k)) for _, k in cols], key=key)
        if keep in self.rows:
            table.move_cursor(row=table.get_row_index(keep))
        self.render_detail(self.selected())

    def selected_key(self) -> Optional[str]:
        table = self.query_one(DataTable)
        if table.row_count == 0:
            return None
        try:
            return table.coordinate_to_cell_key(
                Coordinate(table.cursor_row, 0)
            ).row_key.value
        except Exception:
            return None

    def selected(self) -> Optional[dict]:
        key = self.selected_key()
        return self.rows.get(key) if key is not None else None

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        event.stop()
        row = self.rows.get(event.row_key.value) if event.row_key is not None else None
        if row is not None:
            self.render_detail(row)

    # -- actions --------------------------------------------------------
    async def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        bid = event.button.id or ""
        if bid == "refresh":
            await self.refresh_data()
        elif bid.startswith("act-"):
            self.trigger(bid[4:])

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        await self.refresh_data()

    def trigger(self, action_id: str) -> None:
        """Run an action from the UI: checks the selection, confirms, then works in the background."""
        act = next((a for a in self.ACTIONS if a.id == action_id), None)
        if act is None:
            return
        row = self.selected()
        if act.needs_row and row is None:
            self.say("Select a row first.", error=True)
            return

        def go() -> None:
            self.run_worker(
                self.perform(action_id, row),
                group=f"act-{action_id}",
                exclusive=action_id in self.EXCLUSIVE,
            )

        if act.confirm:
            text = act.confirm.format_map(_Blank(row or {}))
            self.app.push_screen(Confirm(text), lambda yes: go() if yes else None)
        else:
            go()

    EXCLUSIVE: tuple[str, ...] = ()

    async def perform(self, action_id: str, row: Optional[dict] = None) -> None:
        """Do an action now, without confirmation (tests and ``trigger`` use this)."""
        if row is None:
            row = self.selected()
        handler: Callable[..., Any] = getattr(
            self, f"act_{action_id.replace('-', '_')}"
        )
        try:
            keep_view = await handler(row)
        except WorkerCancelled:
            raise
        except ApiError as e:
            self.say(str(e), error=True)
            return
        except Exception as e:  # a bug here must not take the UI down
            self.say(f"{action_id} failed: {e.__class__.__name__}: {e}", error=True)
            return
        if keep_view is not False:
            self.quiet_next = True
            await self.refresh_data()
            self.quiet_next = False
