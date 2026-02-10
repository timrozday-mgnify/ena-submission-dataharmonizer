#!/usr/bin/env python3
"""Interactive LinkML schema editor for DataHarmonizer using Textual.

Provides a terminal-based interface for viewing and editing LinkML schemas.

Features:
- Open schemas from the schemas/ directory
- View/edit fields table (slot_group, required, name, title, description, range, pattern, comments)
- View/edit enums table with hotkey navigation
- Collapse/expand rows by slot_group
- Filter by required fields
- Insert, delete, and edit rows
- Export modified schema to new file

Usage:
    python scripts/edit_linkml.py
    python scripts/edit_linkml.py schemas/ERC000015.yaml

Hotkeys:
    f - Switch to Fields table
    e - Switch to Enums table
    g - Toggle group collapse/expand
    G - Collapse all groups
    ctrl+g - Expand all groups
    r - Toggle required filter
    i - Insert new row
    d - Delete selected row
    Enter - Edit selected cell
    [ - Move field up (decrease rank)
    ] - Move field down (increase rank)
    shift+up/down - Range-select fields
    space - Toggle selection of current row
    escape - Clear selection
    v - View field details
    ctrl+s - Save field detail changes
    s - Save/Export schema
    o - Open schema file
    ctrl+z - Undo
    ctrl+y - Redo
    q - Quit
"""

import copy
import logging
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Optional

import yaml
from rich.text import Text

try:
    from elasticsearch import Elasticsearch
except ImportError:
    Elasticsearch = None

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    Select,
    Static,
    TextArea,
)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

_LOG_NAME = "edit_linkml"


def get_logger() -> logging.Logger:
    """Return the module-level logger.

    Call this from anywhere in the code to obtain the shared logger.
    If :func:`setup_logging` has not been called, the logger will have no
    handlers and messages will be silently discarded (the default
    ``logging`` behaviour for libraries).
    """
    return logging.getLogger(_LOG_NAME)


def setup_logging(log_path: str) -> None:
    """Configure file logging for the application.

    Parameters
    ----------
    log_path:
        Filesystem path where log output will be written.
    """
    logger = logging.getLogger(_LOG_NAME)
    logger.setLevel(logging.DEBUG)
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    )
    logger.addHandler(handler)


# ---------------------------------------------------------------------------
# YAML helpers
# ---------------------------------------------------------------------------

class _LinkMLDumper(yaml.SafeDumper):
    """Custom YAML dumper that emits lowercase booleans and preserves order."""
    pass


def _bool_representer(dumper, data):
    return dumper.represent_scalar("tag:yaml.org,2002:bool", "true" if data else "false")


def _str_representer(dumper, data):
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_LinkMLDumper.add_representer(bool, _bool_representer)
_LinkMLDumper.add_representer(str, _str_representer)

_SELECTED_STYLE = "on dark_blue"
_HIGHLIGHT_STYLE = "bold yellow"


def load_schema(filepath: str) -> dict:
    """Load a LinkML YAML schema file."""
    with open(filepath, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def save_schema(schema: dict, filepath: str) -> None:
    """Save a LinkML schema to YAML file."""
    os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        yaml.dump(
            schema,
            f,
            Dumper=_LinkMLDumper,
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True,
            width=120,
        )


# ---------------------------------------------------------------------------
# Schema data extraction
# ---------------------------------------------------------------------------

def get_main_class(schema: dict) -> tuple[str, dict]:
    """Return (name, class_dict) for the main class (is_a dh_interface)."""
    for name, cls in schema.get("classes", {}).items():
        if isinstance(cls, dict) and cls.get("is_a") == "dh_interface":
            return name, cls
    return "", {}


def extract_fields(schema: dict) -> list[dict]:
    """Extract field data from schema for display in table."""
    main_name, main_cls = get_main_class(schema)
    if not main_cls:
        return []

    slot_usage = main_cls.get("slot_usage", {})
    slots = schema.get("slots", {})
    slot_order = main_cls.get("slots", [])

    fields = []
    for slot_name in slot_order:
        slot_def = slots.get(slot_name, {})
        usage = slot_usage.get(slot_name, {})

        field = {
            "name": slot_name,
            "title": slot_def.get("title", ""),
            "description": slot_def.get("description", ""),
            "range": slot_def.get("range", "string"),
            "pattern": slot_def.get("pattern", ""),
            "required": slot_def.get("required", False),
            "comments": ", ".join(slot_def.get("comments", [])),
            "slot_group": usage.get("slot_group", ""),
            "rank": usage.get("rank", 0),
            "source": slot_def.get("source", ""),
        }
        fields.append(field)

    return fields


def extract_enums(schema: dict) -> list[dict]:
    """Extract enum data from schema for display in table."""
    enums = schema.get("enums", {})
    result = []

    for enum_name, enum_def in enums.items():
        pvs = enum_def.get("permissible_values", {})
        for pv_name, pv_def in pvs.items():
            result.append({
                "enum_name": enum_name,
                "value": pv_name,
                "text": pv_def.get("text", pv_name),
                "description": pv_def.get("description", ""),
            })

    return result


def rebuild_schema(schema: dict, fields: list[dict], enums_data: list[dict]) -> dict:
    """Rebuild schema from modified fields and enums data."""
    main_name, main_cls = get_main_class(schema)

    # Rebuild slots
    new_slots = {}
    new_slot_order = []
    new_slot_usage = {}

    for i, field in enumerate(fields, 1):
        name = field["name"]
        new_slot_order.append(name)

        slot = {"name": name}
        if field.get("title"):
            slot["title"] = field["title"]
        if field.get("description"):
            slot["description"] = field["description"]
        if field.get("range") and field["range"] != "string":
            slot["range"] = field["range"]
        elif field.get("range"):
            slot["range"] = field["range"]
        if field.get("required"):
            slot["required"] = True
        if field.get("pattern"):
            slot["pattern"] = field["pattern"]
        if field.get("comments"):
            comments = [c.strip() for c in field["comments"].split(",") if c.strip()]
            if comments:
                slot["comments"] = comments
        if field.get("source"):
            slot["source"] = field["source"]

        new_slots[name] = slot

        usage = {"rank": i}
        if field.get("slot_group"):
            usage["slot_group"] = field["slot_group"]
        new_slot_usage[name] = usage

    # Rebuild enums
    new_enums = {}
    for enum_row in enums_data:
        enum_name = enum_row["enum_name"]
        if enum_name not in new_enums:
            new_enums[enum_name] = {
                "name": enum_name,
                "permissible_values": {},
            }
        pv = {"text": enum_row.get("text", enum_row["value"])}
        if enum_row.get("description"):
            pv["description"] = enum_row["description"]
        new_enums[enum_name]["permissible_values"][enum_row["value"]] = pv

    # Build new schema
    new_schema = {}
    for key in ["id", "name", "title", "description", "version", "imports", "prefixes", "default_range"]:
        if key in schema:
            new_schema[key] = schema[key]

    # Update main class
    new_main_cls = dict(main_cls)
    new_main_cls["slots"] = new_slot_order
    new_main_cls["slot_usage"] = new_slot_usage

    new_schema["classes"] = {
        "dh_interface": schema.get("classes", {}).get("dh_interface", {
            "name": "dh_interface",
            "description": "A DataHarmonizer interface",
        }),
        main_name: new_main_cls,
    }

    new_schema["slots"] = new_slots

    if new_enums:
        new_schema["enums"] = new_enums

    return new_schema


# ---------------------------------------------------------------------------
# Modal screens
# ---------------------------------------------------------------------------

class FileSelectScreen(ModalScreen[str]):
    """Modal screen for selecting a schema file."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, schemas_dir: str = "schemas"):
        super().__init__()
        self.schemas_dir = schemas_dir

    def compose(self) -> ComposeResult:
        yield Container(
            Label("Select Schema File", id="file-select-title"),
            ListView(id="file-list"),
            Horizontal(
                Button("Cancel", id="cancel-btn", variant="default"),
                id="file-select-buttons",
            ),
            id="file-select-container",
        )

    def on_mount(self) -> None:
        file_list = self.query_one("#file-list", ListView)
        schemas_path = Path(self.schemas_dir)
        if schemas_path.exists():
            for f in sorted(schemas_path.glob("*.yaml")) + sorted(schemas_path.glob("*.yml")):
                file_list.append(ListItem(Label(f.name), id=f"file-{f.name}"))

    @on(ListView.Selected)
    def on_file_selected(self, event: ListView.Selected) -> None:
        if event.item:
            label = event.item.query_one(Label)
            filename = str(label.renderable)
            self.dismiss(os.path.join(self.schemas_dir, filename))

    @on(Button.Pressed, "#cancel-btn")
    def on_cancel(self) -> None:
        self.dismiss("")

    def action_cancel(self) -> None:
        self.dismiss("")


class SaveScreen(ModalScreen[str]):
    """Modal screen for entering save filename."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, default_name: str = ""):
        super().__init__()
        self.default_name = default_name

    def compose(self) -> ComposeResult:
        yield Container(
            Label("Export Schema", id="save-title"),
            Label("Enter filename:"),
            Input(value=self.default_name, id="save-input", placeholder="schema.yaml"),
            Horizontal(
                Button("Save", id="save-btn", variant="primary"),
                Button("Cancel", id="cancel-btn", variant="default"),
                id="save-buttons",
            ),
            id="save-container",
        )

    def on_mount(self) -> None:
        self.query_one("#save-input", Input).focus()

    @on(Button.Pressed, "#save-btn")
    def on_save(self) -> None:
        filename = self.query_one("#save-input", Input).value
        if filename:
            self.dismiss(filename)

    @on(Button.Pressed, "#cancel-btn")
    def on_cancel(self) -> None:
        self.dismiss("")

    @on(Input.Submitted)
    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.value:
            self.dismiss(event.value)

    def action_cancel(self) -> None:
        self.dismiss("")


class EditCellScreen(ModalScreen[str]):
    """Modal screen for editing a cell value."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "save", "Save"),
    ]

    def __init__(self, column: str, value: str):
        super().__init__()
        self.column = column
        self.initial_value = value

    def compose(self) -> ComposeResult:
        yield Container(
            Label(f"Edit: {self.column}", id="edit-title"),
            TextArea(self.initial_value, id="edit-area"),
            Horizontal(
                Button("Save", id="save-btn", variant="primary"),
                Button("Cancel", id="cancel-btn", variant="default"),
                id="edit-buttons",
            ),
            id="edit-container",
        )

    def on_mount(self) -> None:
        self.query_one("#edit-area", TextArea).focus()

    @on(Button.Pressed, "#save-btn")
    def on_save(self) -> None:
        value = self.query_one("#edit-area", TextArea).text
        self.dismiss(value)

    @on(Button.Pressed, "#cancel-btn")
    def on_cancel(self) -> None:
        self.dismiss(self.initial_value)

    def action_cancel(self) -> None:
        self.dismiss(self.initial_value)

    def action_save(self) -> None:
        value = self.query_one("#edit-area", TextArea).text
        self.dismiss(value)


class NewFieldScreen(ModalScreen[dict]):
    """Modal screen for adding a new field."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, slot_groups: list[str]):
        super().__init__()
        self.slot_groups = slot_groups

    def compose(self) -> ComposeResult:
        yield Container(
            Label("Add New Field", id="new-field-title"),
            Horizontal(Label("Name:"), Input(id="name-input", placeholder="field_name")),
            Horizontal(Label("Title:"), Input(id="title-input", placeholder="Field Title")),
            Horizontal(Label("Description:"), Input(id="desc-input", placeholder="Field description")),
            Horizontal(Label("Range:"), Input(id="range-input", value="string", placeholder="string")),
            Horizontal(Label("Slot Group:"), Input(id="group-input", placeholder="Group name")),
            Horizontal(
                Button("Add", id="add-btn", variant="primary"),
                Button("Cancel", id="cancel-btn", variant="default"),
                id="new-field-buttons",
            ),
            id="new-field-container",
        )

    def on_mount(self) -> None:
        self.query_one("#name-input", Input).focus()

    @on(Button.Pressed, "#add-btn")
    def on_add(self) -> None:
        name = self.query_one("#name-input", Input).value
        if name:
            field = {
                "name": name,
                "title": self.query_one("#title-input", Input).value,
                "description": self.query_one("#desc-input", Input).value,
                "range": self.query_one("#range-input", Input).value or "string",
                "slot_group": self.query_one("#group-input", Input).value,
                "required": False,
                "pattern": "",
                "comments": "",
                "rank": 0,
            }
            self.dismiss(field)
        else:
            self.notify("Field name is required", severity="error")

    @on(Button.Pressed, "#cancel-btn")
    def on_cancel(self) -> None:
        self.dismiss({})

    def action_cancel(self) -> None:
        self.dismiss({})


class NewEnumValueScreen(ModalScreen[dict]):
    """Modal screen for adding a new enum value."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, enum_names: list[str]):
        super().__init__()
        self.enum_names = enum_names

    def compose(self) -> ComposeResult:
        yield Container(
            Label("Add New Enum Value", id="new-enum-title"),
            Horizontal(Label("Enum Name:"), Input(id="enum-name-input", placeholder="EnumNameMenu")),
            Horizontal(Label("Value:"), Input(id="value-input", placeholder="value")),
            Horizontal(Label("Text:"), Input(id="text-input", placeholder="Display text")),
            Horizontal(Label("Description:"), Input(id="desc-input", placeholder="Description")),
            Horizontal(
                Button("Add", id="add-btn", variant="primary"),
                Button("Cancel", id="cancel-btn", variant="default"),
                id="new-enum-buttons",
            ),
            id="new-enum-container",
        )

    def on_mount(self) -> None:
        self.query_one("#enum-name-input", Input).focus()

    @on(Button.Pressed, "#add-btn")
    def on_add(self) -> None:
        enum_name = self.query_one("#enum-name-input", Input).value
        value = self.query_one("#value-input", Input).value
        if enum_name and value:
            enum_row = {
                "enum_name": enum_name,
                "value": value,
                "text": self.query_one("#text-input", Input).value or value,
                "description": self.query_one("#desc-input", Input).value,
            }
            self.dismiss(enum_row)
        else:
            self.notify("Enum name and value are required", severity="error")

    @on(Button.Pressed, "#cancel-btn")
    def on_cancel(self) -> None:
        self.dismiss({})

    def action_cancel(self) -> None:
        self.dismiss({})


class ConfirmScreen(ModalScreen[bool]):
    """Modal screen for confirmation dialogs."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("y", "confirm", "Yes"),
        Binding("n", "cancel", "No"),
    ]

    def __init__(self, message: str):
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        yield Container(
            Label(self.message, id="confirm-message"),
            Horizontal(
                Button("Yes", id="yes-btn", variant="warning"),
                Button("No", id="no-btn", variant="default"),
                id="confirm-buttons",
            ),
            id="confirm-container",
        )

    @on(Button.Pressed, "#yes-btn")
    def on_yes(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#no-btn")
    def on_no(self) -> None:
        self.dismiss(False)

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


# ---------------------------------------------------------------------------
# Field detail view helpers
# ---------------------------------------------------------------------------

_DETAIL_ATTRS = [
    ("name", "Name"),
    ("title", "Title"),
    ("slot_group", "Slot Group"),
    ("source", "Source"),
    ("rank", "Rank"),
    ("required", "Required"),
    ("range", "Range"),
    ("pattern", "Pattern"),
    ("description", "Description"),
    ("comments", "Comments"),
]


class _DetailTextArea(TextArea):
    """TextArea that does not handle undo/redo, allowing app-level handling."""
    BINDINGS = []


# ---------------------------------------------------------------------------
# Main Application
# ---------------------------------------------------------------------------

class LinkMLEditor(App):
    """Interactive LinkML schema editor."""

    CSS = """
    Screen {
        background: $surface;
    }

    #main-container {
        width: 100%;
        height: 100%;
    }

    #status-bar {
        dock: top;
        height: 3;
        background: $primary;
        padding: 0 1;
    }

    #status-bar Label {
        margin-right: 2;
    }

    #table-container {
        height: 100%;
    }

    DataTable {
        height: 100%;
    }

    /* Modal styles */
    #file-select-container, #save-container, #edit-container,
    #new-field-container, #new-enum-container, #confirm-container {
        width: 60;
        height: auto;
        max-height: 80%;
        background: $surface;
        border: solid $primary;
        padding: 1 2;
    }

    #file-select-title, #save-title, #edit-title, #new-field-title,
    #new-enum-title, #confirm-message {
        text-style: bold;
        margin-bottom: 1;
    }

    #file-list {
        height: 15;
        margin-bottom: 1;
    }

    #edit-area {
        height: 10;
        margin-bottom: 1;
    }

    #file-select-buttons, #save-buttons, #edit-buttons,
    #new-field-buttons, #new-enum-buttons, #confirm-buttons {
        align: center middle;
        margin-top: 1;
    }

    #new-field-container Horizontal, #new-enum-container Horizontal {
        height: 3;
        margin-bottom: 0;
    }

    #new-field-container Horizontal Label, #new-enum-container Horizontal Label {
        width: 15;
    }

    #new-field-container Horizontal Input, #new-enum-container Horizontal Input {
        width: 100%;
    }

    Button {
        margin: 0 1;
    }

    .hidden {
        display: none;
    }

    #search-input {
        height: 3;
        margin: 0 1;
    }

    #field-detail-container {
        padding: 0 2;
    }

    .detail-label {
        text-style: bold;
        margin-top: 1;
    }

    #field-detail-container TextArea {
        height: 3;
    }

    #field-detail-container .detail-large {
        height: 8;
    }

    #detail-header {
        text-style: bold;
        text-align: center;
        margin: 1 0;
    }

    #detail-buttons {
        margin-top: 1;
        align: center middle;
    }
    """

    # Column width caps for the three cycling states (default / expanded / collapsed).
    _COL_MAX_DEFAULT = 80
    _COL_MAX_EXPANDED = 1000

    # Column definitions: (label, key)
    _FIELD_COLUMNS = [
        (" ", "_grp"),
        ("rank", "rank"),
        ("slot_group", "slot_group"),
        ("source", "source"),
        ("required", "required"),
        ("name", "name"),
        ("title", "title"),
        ("description", "description"),
        ("range", "range"),
        ("pattern", "pattern"),
        ("comments", "comments"),
    ]

    BINDINGS = [
        Binding("f", "show_fields", "Fields"),
        Binding("e", "show_enums", "Enums"),
        Binding("g", "toggle_groups", "Toggle Groups"),
        Binding("G", "collapse_all_groups", "Collapse All", show=False),
        Binding("ctrl+g", "expand_all_groups", "Expand All", show=False),
        Binding("r", "toggle_required", "Filter Required"),
        Binding("i", "insert_row", "Insert"),
        Binding("d", "delete_row", "Delete"),
        Binding("enter", "edit_cell", "Edit"),
        Binding("[", "rank_up", "Rank Up"),
        Binding("]", "rank_down", "Rank Down"),
        Binding("{", "rank_top", "Rank Top", show=False),
        Binding("}", "rank_bottom", "Rank Bottom", show=False),
        Binding("shift+up", "select_extend_up", "Select Up", show=False),
        Binding("shift+down", "select_extend_down", "Select Down", show=False),
        Binding("space", "toggle_select", "Toggle Select", show=False),
        Binding("escape", "clear_selection", "Clear Selection", show=False),
        Binding("/", "focus_search", "Search"),
        Binding("v", "view_field", "View Field"),
        Binding("ctrl+s", "save_field_detail", "Save Field", show=False),
        Binding("s", "save_schema", "Save"),
        Binding("o", "open_schema", "Open"),
        Binding("ctrl+z", "undo", "Undo"),
        Binding("ctrl+y", "redo", "Redo"),
        Binding("q", "quit_app", "Quit"),
    ]

    def __init__(self, initial_file: Optional[str] = None, es_url: Optional[str] = None):
        super().__init__()
        self.schema: dict = {}
        self.fields: list[dict] = []
        self.enums_data: list[dict] = []
        self.current_file: str = ""
        self.initial_file = initial_file
        self._es_url: str = es_url or "http://localhost:9200"
        self.current_view = "fields"  # "fields" or "enums"
        self.filter_required = False
        self.collapsed_groups: set[str] = set()
        self.modified = False
        # Undo/redo stacks store (fields, enums_data) tuples
        self._undo_stack: list[tuple[list[dict], list[dict]]] = []
        self._redo_stack: list[tuple[list[dict], list[dict]]] = []
        self._max_history = 50  # Limit history size
        # Track last cursor positions for view switching
        self._last_fields_row_key: Optional[str] = None
        self._last_enums_row_key: Optional[str] = None
        # Multi-select state for fields view
        self._selected_fields: set[str] = set()
        self._selection_anchor: Optional[str] = None
        # Field detail view state
        self._detail_field_name: Optional[str] = None
        # Column width cycling state: 0=default(80), 1=expanded(1000), 2=collapsed(header)
        self._col_width_state: dict[str, int] = {}
        # Elasticsearch search state
        self._es: Optional[object] = None
        self._es_index: str = f"linkml_editor_{uuid.uuid4().hex[:8]}"
        self._es_dirty: bool = True
        self._search_matched: Optional[set[str]] = None
        self._search_highlights: dict[str, dict[str, list[str]]] = {}

    def compose(self) -> ComposeResult:
        yield Header()
        yield Container(
            Horizontal(
                Label("File: None", id="file-label"),
                Label("View: Fields", id="view-label"),
                Label("", id="filter-label"),
                id="status-bar",
            ),
            Input(placeholder="Search (ES syntax: field:value, AND, OR, *)...", id="search-input"),
            VerticalScroll(
                DataTable(id="fields-table"),
                DataTable(id="enums-table", classes="hidden"),
                VerticalScroll(
                    Label("", id="detail-header"),
                    Label("Name", classes="detail-label"),
                    _DetailTextArea(id="detail-attr-name", soft_wrap=True),
                    Label("Title", classes="detail-label"),
                    _DetailTextArea(id="detail-attr-title", soft_wrap=True),
                    Label("Slot Group", classes="detail-label"),
                    _DetailTextArea(id="detail-attr-slot_group", soft_wrap=True),
                    Label("Source", classes="detail-label"),
                    _DetailTextArea(id="detail-attr-source", soft_wrap=True),
                    Label("Rank", classes="detail-label"),
                    _DetailTextArea(id="detail-attr-rank", soft_wrap=True),
                    Label("Required", classes="detail-label"),
                    _DetailTextArea(id="detail-attr-required", soft_wrap=True),
                    Label("Range", classes="detail-label"),
                    _DetailTextArea(id="detail-attr-range", soft_wrap=True),
                    Label("Pattern", classes="detail-label"),
                    _DetailTextArea(id="detail-attr-pattern", soft_wrap=True),
                    Label("Description", classes="detail-label"),
                    _DetailTextArea(id="detail-attr-description", soft_wrap=True, classes="detail-large"),
                    Label("Comments", classes="detail-label"),
                    _DetailTextArea(id="detail-attr-comments", soft_wrap=True, classes="detail-large"),
                    Horizontal(
                        Button("Save (Ctrl+S)", id="detail-save-btn", variant="primary"),
                        Button("Discard (Esc)", id="detail-discard-btn", variant="default"),
                        id="detail-buttons",
                    ),
                    id="field-detail-container",
                    classes="hidden",
                ),
                id="table-container",
            ),
            id="main-container",
        )
        yield Footer()

    def on_mount(self) -> None:
        # Setup fields table
        fields_table = self.query_one("#fields-table", DataTable)
        fields_table.cursor_type = "row"
        for label, key in self._FIELD_COLUMNS:
            fields_table.add_column(label, key=key)

        # Setup enums table
        enums_table = self.query_one("#enums-table", DataTable)
        enums_table.cursor_type = "row"
        enums_table.add_columns("enum_name", "value", "text", "description")

        # Initialize Elasticsearch for search
        self._init_elasticsearch()

        # Load initial file if provided
        if self.initial_file and os.path.exists(self.initial_file):
            self.load_file(self.initial_file)
        else:
            # Prompt to open a file
            self.call_after_refresh(self.action_open_schema)

    def load_file(self, filepath: str) -> None:
        """Load a schema file and populate tables."""
        try:
            self.schema = load_schema(filepath)
            self.current_file = filepath
            self.fields = extract_fields(self.schema)
            self.enums_data = extract_enums(self.schema)
            self.modified = False
            self.collapsed_groups = set()
            # Clear undo/redo history on file load
            self._undo_stack.clear()
            self._redo_stack.clear()
            # Clear saved cursor positions
            self._last_fields_row_key = None
            self._last_enums_row_key = None
            self._selected_fields.clear()
            self._selection_anchor = None
            self._detail_field_name = None
            self._search_matched = None
            self._search_highlights = {}
            self._es_dirty = True
            try:
                self.query_one("#search-input", Input).value = ""
            except Exception:
                pass
            self._index_fields()

            self.refresh_fields_table()
            self.refresh_enums_table()
            self.update_status()

            self.notify(f"Loaded: {filepath}")
        except Exception as e:
            self.notify(f"Error loading file: {e}", severity="error")

    @staticmethod
    def _get_display_group(field: dict) -> str:
        """Return the display group for a field.

        Uses ``slot_group`` if it has a value, otherwise falls back to
        ``source``.  This determines how rows are visually grouped,
        collapsed, and expanded in the fields table.
        """
        return field.get("slot_group") or field.get("source") or ""

    def _renumber_ranks(self) -> None:
        """Reassign sequential ranks (1, 2, 3, …) based on current rank order."""
        for new_rank, field in enumerate(
            sorted(self.fields, key=lambda f: f.get("rank", 0)), start=1
        ):
            field["rank"] = new_rank

    def _sorted_fields(self) -> list[dict]:
        """Return fields sorted by display group then rank.

        Groups are ordered by the lowest rank among their members.
        """
        # Determine each group's sort key: its minimum rank value.
        group_min_rank: dict[str, int] = {}
        for field in self.fields:
            group = self._get_display_group(field)
            rank = field.get("rank", 0)
            if group not in group_min_rank or rank < group_min_rank[group]:
                group_min_rank[group] = rank

        return sorted(
            self.fields,
            key=lambda f: (group_min_rank.get(self._get_display_group(f), 0), f.get("rank", 0)),
        )

    def refresh_fields_table(self) -> None:
        """Refresh the fields table with current data."""
        table = self.query_one("#fields-table", DataTable)
        table.clear()

        # Re-run search if index is stale and search is active
        if self._es_dirty and self._search_matched is not None:
            try:
                query = self.query_one("#search-input", Input).value
                if query.strip():
                    self._run_search(query)
            except Exception:
                pass

        sorted_fields = self._sorted_fields()

        # Track max content width per column index for width capping.
        col_max_widths: list[int] = [0] * len(self._FIELD_COLUMNS)

        # Precompute total field count per group (unfiltered).
        group_counts: dict[str, int] = {}
        for f in self.fields:
            g = self._get_display_group(f)
            group_counts[g] = group_counts.get(g, 0) + 1

        seen_groups: set[str] = set()

        for field in sorted_fields:
            group = self._get_display_group(field)
            is_first_in_group = group not in seen_groups

            # --- collapsed group: show one summary row, skip the rest ----
            if group and group in self.collapsed_groups:
                if not is_first_in_group:
                    continue
                seen_groups.add(group)
                # Skip collapsed group if search active and no fields match
                if self._search_matched is not None:
                    group_names = [
                        f.get("name", "") for f in self.fields
                        if self._get_display_group(f) == group
                    ]
                    if not any(n in self._search_matched for n in group_names):
                        continue
                name = field.get("name", "")
                count = group_counts.get(group, 0)
                summary_texts = [
                    "▶", "", f"{group} ({count} fields)",
                    "", "", "", "", "", "", "", "",
                ]
                for ci, cv in enumerate(summary_texts):
                    col_max_widths[ci] = max(col_max_widths[ci], len(str(cv)))
                cells = summary_texts
                if name in self._selected_fields:
                    cells = [Text(str(c), style=_SELECTED_STYLE) for c in cells]
                table.add_row(*cells, key=name)
                continue

            # --- search filter -------------------------------------------
            name = field.get("name", "")
            if self._search_matched is not None and name not in self._search_matched:
                continue

            # --- required filter (only for non-collapsed groups) ---------
            if self.filter_required and not field.get("required"):
                continue

            # Track first *visible* row per group for the expand indicator.
            is_first_visible = group not in seen_groups
            if is_first_visible:
                seen_groups.add(group)

            grp_indicator = "▼" if is_first_visible and group else ""

            # Build cells with optional search highlighting
            field_hl = self._search_highlights.get(name, {})
            is_selected = name in self._selected_fields
            base_style = _SELECTED_STYLE if is_selected else ""

            raw_cells = [
                (None, grp_indicator),
                ("rank", str(field.get("rank", 0))),
                ("slot_group", group),
                ("source", field.get("source", "")),
                ("required", "Yes" if field.get("required") else "No"),
                ("name", name),
                ("title", field.get("title", "")),
                ("description", self._truncate(field.get("description", ""), 50)),
                ("range", field.get("range", "string")),
                ("pattern", field.get("pattern", "")),
                ("comments", self._truncate(field.get("comments", ""), 30)),
            ]
            for ci, (_, cv) in enumerate(raw_cells):
                col_max_widths[ci] = max(col_max_widths[ci], len(str(cv)))

            cells = []
            for es_key, text_val in raw_cells:
                terms = set()
                if es_key and es_key in field_hl:
                    terms = self._extract_highlight_terms(field_hl[es_key])
                if terms:
                    cells.append(self._highlight_cell(str(text_val), terms, base_style))
                elif is_selected:
                    cells.append(Text(str(text_val), style=_SELECTED_STYLE))
                else:
                    cells.append(text_val)

            table.add_row(*cells, key=name)

        self._apply_column_widths(table, col_max_widths)

    def _apply_column_widths(
        self, table: DataTable, content_widths: list[int]
    ) -> None:
        """Set column widths according to the current cycling state.

        State 0 (default):  min(content_width, 80)
        State 1 (expanded): min(content_width, 1000)
        State 2 (collapsed): width of the column header label
        """
        columns = list(table.columns.values())
        for i, col in enumerate(columns):
            if i >= len(self._FIELD_COLUMNS):
                break
            key = self._FIELD_COLUMNS[i][1]
            label_text = self._FIELD_COLUMNS[i][0]
            state = self._col_width_state.get(key, 0)
            natural = content_widths[i] if i < len(content_widths) else 0
            label_width = len(label_text)

            if state == 2:
                w = label_width
            elif state == 1:
                w = min(natural, self._COL_MAX_EXPANDED)
            else:
                w = min(natural, self._COL_MAX_DEFAULT)

            # Ensure at least the label fits.
            w = max(w, label_width)
            col.auto_width = False
            col.width = w
            col.content_width = w

    @on(DataTable.HeaderSelected, "#fields-table")
    def _on_fields_header_selected(self, event: DataTable.HeaderSelected) -> None:
        """Cycle column width on header click: default -> expanded -> collapsed."""
        col_key_str = str(event.column_key.value)
        # Map the column_key back to our key name
        key: str | None = None
        for _, k in self._FIELD_COLUMNS:
            if k == col_key_str:
                key = k
                break
        if key is None:
            return
        table = event.data_table
        saved_scroll_x = table.scroll_x
        saved_scroll_y = table.scroll_y
        state = self._col_width_state.get(key, 0)
        self._col_width_state[key] = (state + 1) % 3
        self.refresh_fields_table()
        self.call_after_refresh(
            lambda: table.scroll_to(x=saved_scroll_x, y=saved_scroll_y, animate=False)
        )

    def on_click(self, event) -> None:
        """Toggle group collapse/expand when the arrow indicator is clicked."""
        if self.current_view != "fields":
            return
        try:
            table = self.query_one("#fields-table", DataTable)
        except Exception:
            return
        # Only act when the mouse is over the fields table's first column.
        if not table.mouse_hover:
            return
        if table.hover_coordinate.column != 0:
            return
        hover_row = table.hover_coordinate.row
        row_key = self._get_row_key_at(table, hover_row)
        if not row_key:
            return
        field = next((f for f in self.fields if f["name"] == row_key), None)
        if not field:
            return
        group = self._get_display_group(field)
        if group:
            self._toggle_group(group)

    def refresh_enums_table(self) -> None:
        """Refresh the enums table with current data."""
        table = self.query_one("#enums-table", DataTable)
        table.clear()

        for i, enum_row in enumerate(self.enums_data):
            table.add_row(
                enum_row.get("enum_name", ""),
                enum_row.get("value", ""),
                enum_row.get("text", ""),
                self._truncate(enum_row.get("description", ""), 50),
                key=f"{enum_row.get('enum_name')}_{i}",
            )

    def _truncate(self, text: str, max_len: int) -> str:
        """Truncate text for display."""
        if len(text) > max_len:
            return text[:max_len - 3] + "..."
        return text

    def _get_row_key_at(self, table: DataTable, row_idx: int) -> Optional[str]:
        """Get the row key (as string) at the given row index.

        Note: table.get_row_at() returns row DATA, not the key.
        We need to use ordered_rows to get the actual RowKey object.
        """
        if row_idx < 0 or row_idx >= table.row_count:
            return None
        row_obj = table.ordered_rows[row_idx]
        return str(row_obj.key.value) if row_obj.key.value else None

    def _move_cursor_to_key(self, table: DataTable, row_key: str) -> bool:
        """Move table cursor to the row with the given key. Returns True if found."""
        if table.row_count == 0:
            return False
        # Iterate through rows to find the matching key
        for row_idx in range(table.row_count):
            key = self._get_row_key_at(table, row_idx)
            if key and key == row_key:
                table.move_cursor(row=row_idx)
                return True
        return False

    def update_status(self) -> None:
        """Update status bar labels."""
        file_label = self.query_one("#file-label", Label)
        view_label = self.query_one("#view-label", Label)
        filter_label = self.query_one("#filter-label", Label)

        filename = os.path.basename(self.current_file) if self.current_file else "None"
        modified = " *" if self.modified else ""
        file_label.update(f"File: {filename}{modified}")

        if self.current_view == "field_detail":
            view_label.update(f"View: Field Detail ({self._detail_field_name or ''})")
        else:
            view_label.update(f"View: {self.current_view.title()}")

        filters = []
        if self.filter_required:
            filters.append("Required only")
        if self.collapsed_groups:
            filters.append(f"{len(self.collapsed_groups)} groups collapsed")
        filter_label.update(" | ".join(filters))

    def _save_state(self) -> None:
        """Save current state to undo stack before making changes."""
        # Deep copy current state
        state = (
            copy.deepcopy(self.fields),
            copy.deepcopy(self.enums_data),
        )
        self._undo_stack.append(state)
        # Limit stack size
        if len(self._undo_stack) > self._max_history:
            self._undo_stack.pop(0)
        # Clear redo stack on new action
        self._redo_stack.clear()
        self._es_dirty = True

    # ------------------------------------------------------------------
    # Elasticsearch search
    # ------------------------------------------------------------------

    def _init_elasticsearch(self) -> None:
        """Try to connect to a local Elasticsearch instance."""
        if Elasticsearch is None:
            self.notify("elasticsearch package not installed – search disabled", severity="warning")
            return
        try:
            self._es = Elasticsearch(self._es_url)
            self._es.info()  # type: ignore[union-attr]
        except Exception as exc:
            self._es = None
            self.notify(f"Elasticsearch unavailable ({self._es_url}) – search disabled", severity="warning")

    def _cleanup_elasticsearch(self) -> None:
        """Delete the ES index on shutdown."""
        if self._es:
            try:
                self._es.options(ignore_status=[404]).indices.delete(index=self._es_index)  # type: ignore[union-attr]
            except Exception:
                pass

    def _index_fields(self) -> None:
        """Index all fields into Elasticsearch."""
        if not self._es:
            return
        try:
            if self._es.indices.exists(index=self._es_index):  # type: ignore[union-attr]
                self._es.indices.delete(index=self._es_index)  # type: ignore[union-attr]
            # Custom analyzers:
            # - underscore_analyzer: splits on underscores/delimiters for
            #   whole-word matching (e.g. name:sample).
            # - partial_analyzer / underscore_partial_analyzer: use edge_ngram
            #   at index time so that prefix/sub-word queries like "coll" match
            #   "collection".  The search_analyzer is set to the non-ngram
            #   variant so the search term is not itself expanded.
            self._es.indices.create(  # type: ignore[union-attr]
                index=self._es_index,
                settings={
                    "analysis": {
                        "tokenizer": {
                            "underscore_tokenizer": {
                                "type": "pattern",
                                "pattern": r"[_\s\-\.]+",
                            }
                        },
                        "filter": {
                            "edge_ngram_filter": {
                                "type": "edge_ngram",
                                "min_gram": 2,
                                "max_gram": 20,
                            }
                        },
                        "analyzer": {
                            "underscore_analyzer": {
                                "type": "pattern",
                                "pattern": r"[_\s\-\.]+",
                                "lowercase": True,
                            },
                            "partial_analyzer": {
                                "type": "custom",
                                "tokenizer": "standard",
                                "filter": ["lowercase", "edge_ngram_filter"],
                            },
                            "underscore_partial_analyzer": {
                                "type": "custom",
                                "tokenizer": "underscore_tokenizer",
                                "filter": ["lowercase", "edge_ngram_filter"],
                            },
                        }
                    }
                },
                mappings={
                    "properties": {
                        "name": {"type": "text",
                                 "analyzer": "underscore_partial_analyzer",
                                 "search_analyzer": "underscore_analyzer",
                                 "fields": {"keyword": {"type": "keyword"}}},
                        "title": {"type": "text",
                                  "analyzer": "partial_analyzer",
                                  "search_analyzer": "standard",
                                  "fields": {"keyword": {"type": "keyword"}}},
                        "description": {"type": "text",
                                        "analyzer": "partial_analyzer",
                                        "search_analyzer": "standard",
                                        "fields": {"keyword": {"type": "keyword"}}},
                        "range": {"type": "text",
                                  "analyzer": "underscore_partial_analyzer",
                                  "search_analyzer": "underscore_analyzer",
                                  "fields": {"keyword": {"type": "keyword"}}},
                        "pattern": {"type": "text",
                                    "fields": {"keyword": {"type": "keyword"}}},
                        "required": {"type": "text",
                                     "fields": {"keyword": {"type": "keyword"}}},
                        "comments": {"type": "text",
                                     "analyzer": "partial_analyzer",
                                     "search_analyzer": "standard",
                                     "fields": {"keyword": {"type": "keyword"}}},
                        "slot_group": {"type": "text",
                                       "analyzer": "underscore_partial_analyzer",
                                       "search_analyzer": "underscore_analyzer",
                                       "fields": {"keyword": {"type": "keyword"}}},
                        "source": {"type": "text",
                                   "analyzer": "underscore_partial_analyzer",
                                   "search_analyzer": "underscore_analyzer",
                                   "fields": {"keyword": {"type": "keyword"}}},
                        "rank": {"type": "text",
                                 "fields": {"keyword": {"type": "keyword"}}},
                    }
                },
            )
            for field in self.fields:
                doc = {
                    "name": field.get("name", ""),
                    "title": field.get("title", ""),
                    "description": field.get("description", ""),
                    "range": field.get("range", ""),
                    "pattern": field.get("pattern", ""),
                    "required": "Yes" if field.get("required") else "No",
                    "comments": field.get("comments", ""),
                    "slot_group": field.get("slot_group", ""),
                    "source": field.get("source", ""),
                    "rank": str(field.get("rank", 0)),
                }
                self._es.index(index=self._es_index, id=field["name"], document=doc)  # type: ignore[union-attr]
            self._es.indices.refresh(index=self._es_index)  # type: ignore[union-attr]
            self._es_dirty = False
        except Exception as exc:
            self.notify(f"Search index error: {exc}", severity="warning")

    _SEARCHABLE_FIELDS = [
        "name", "title", "description", "range",
        "pattern", "slot_group", "source", "comments",
        "required", "rank",
    ]

    @staticmethod
    def _escape_es_regexp(s: str) -> str:
        """Escape special characters for an Elasticsearch regexp query.

        Only the standard Lucene regexp operators need escaping:
        . ? + * | { } [ ] ( ) \\
        """
        out: list[str] = []
        for ch in s:
            if ch in r".?+*|{}[]()\\":
                out.append("\\")
            out.append(ch)
        return "".join(out)

    def _run_search(self, query: str) -> None:
        """Execute ES regexp search and update match/highlight state.

        Every whitespace-separated token is converted to the regexp
        ``.*<escaped_token>.*`` and matched against the ``.keyword``
        sub-field of every searchable attribute.  This gives true
        substring matching (including single-character and
        cross-delimiter queries such as ``22:sa`` matching
        ``ERC000022:sample``).

        Tokens that start with a known field name followed by ``:``
        (e.g. ``name:coll``) are treated as field-specific searches.
        """
        if not query.strip():
            self._search_matched = None
            self._search_highlights = {}
            return
        if not self._es:
            return
        if self._es_dirty:
            self._index_fields()
        try:
            known = {f.lower() for f in self._SEARCHABLE_FIELDS}
            tokens = query.strip().split()
            must_clauses: list[dict] = []
            highlight_terms: list[str] = []

            for token in tokens:
                field_name: str | None = None
                value = token

                # Detect field-specific syntax (field:value)
                if ":" in token:
                    prefix, suffix = token.split(":", 1)
                    if prefix.lower() in known:
                        field_name = prefix.lower()
                        value = suffix

                if not value:
                    continue

                escaped = self._escape_es_regexp(value)
                pattern = f".*{escaped}.*"

                if field_name:
                    must_clauses.append({
                        "regexp": {
                            f"{field_name}.keyword": {
                                "value": pattern,
                                "case_insensitive": True,
                            }
                        }
                    })
                else:
                    should_clauses = [
                        {"regexp": {
                            f"{f}.keyword": {
                                "value": pattern,
                                "case_insensitive": True,
                            }
                        }}
                        for f in self._SEARCHABLE_FIELDS
                    ]
                    must_clauses.append({
                        "bool": {"should": should_clauses, "minimum_should_match": 1}
                    })

                highlight_terms.append(value)

            if not must_clauses:
                self._search_matched = None
                self._search_highlights = {}
                return

            es_query = (
                {"bool": {"must": must_clauses}}
                if len(must_clauses) > 1
                else must_clauses[0]
            )

            resp = self._es.search(  # type: ignore[union-attr]
                index=self._es_index,
                query=es_query,
                size=10000,
            )

            matched: set[str] = set()
            highlights: dict[str, dict[str, list[str]]] = {}
            # Build client-side highlight fragments from the search terms
            hl_fragments = {
                f: [f"<em>{t}</em>" for t in highlight_terms]
                for f in self._SEARCHABLE_FIELDS
            }
            for hit in resp["hits"]["hits"]:
                name = hit["_id"]
                matched.add(name)
                highlights[name] = hl_fragments

            self._search_matched = matched
            self._search_highlights = highlights
        except Exception:
            # Regexp errors while typing are expected – keep the previous
            # search state rather than clearing it.
            pass

    def _perform_search(self, query: str) -> None:
        """Search fields and refresh the table."""
        self._run_search(query)
        self.refresh_fields_table()

    @staticmethod
    def _extract_highlight_terms(fragments: list[str]) -> set[str]:
        """Extract matched terms from ES highlight fragments."""
        terms: set[str] = set()
        for fragment in fragments:
            for m in re.finditer(r"<em>(.*?)</em>", fragment):
                terms.add(m.group(1))
        return terms

    def _highlight_cell(self, text: str, terms: set[str], base_style: str = "") -> Text:
        """Create Rich Text with highlighted search terms."""
        if not terms or not text:
            return Text(text, style=base_style) if base_style else Text(text)

        # Build (start, end) ranges to highlight
        ranges: list[tuple[int, int]] = []
        text_lower = text.lower()
        for term in terms:
            term_lower = term.lower()
            start = 0
            while True:
                idx = text_lower.find(term_lower, start)
                if idx == -1:
                    break
                ranges.append((idx, idx + len(term)))
                start = idx + 1

        if not ranges:
            return Text(text, style=base_style) if base_style else Text(text)

        # Merge overlapping ranges
        ranges.sort()
        merged = [ranges[0]]
        for s, e in ranges[1:]:
            if s <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], e))
            else:
                merged.append((s, e))

        # Build Rich Text
        result = Text()
        hl_style = _HIGHLIGHT_STYLE + (" " + base_style if base_style else "")
        pos = 0
        for s, e in merged:
            if pos < s:
                result.append(text[pos:s], style=base_style or "")
            result.append(text[s:e], style=hl_style)
            pos = e
        if pos < len(text):
            result.append(text[pos:], style=base_style or "")

        return result

    def action_focus_search(self) -> None:
        """Focus the search input."""
        if self.current_view == "fields":
            self.query_one("#search-input", Input).focus()

    @on(Input.Changed, "#search-input")
    def _on_search_changed(self, event: Input.Changed) -> None:
        """Handle search query changes."""
        self._perform_search(event.value)

    @on(Input.Submitted, "#search-input")
    def _on_search_submitted(self, event: Input.Submitted) -> None:
        """Return focus to the fields table on Enter."""
        if self.current_view == "fields":
            self.query_one("#fields-table", DataTable).focus()

    def _restore_cursor(self, table: DataTable, row_key: Optional[str], row_idx: Optional[int]) -> None:
        """Try to place the cursor on *row_key*; fall back to *row_idx*."""
        if row_key and self._move_cursor_to_key(table, row_key):
            return
        if row_idx is not None and table.row_count > 0:
            table.move_cursor(row=min(row_idx, table.row_count - 1))

    def action_undo(self) -> None:
        """Undo the last edit."""
        if not self._undo_stack:
            self.notify("Nothing to undo")
            return

        # Remember cursor position in the active table.
        row_key = None
        row_idx = None
        if self.current_view in ("fields", "enums"):
            table = self.query_one(f"#{self.current_view}-table", DataTable)
            row_key = self._get_row_key_at(table, table.cursor_row) if table.cursor_row is not None else None
            row_idx = table.cursor_row

        # Save current state to redo stack
        current_state = (
            copy.deepcopy(self.fields),
            copy.deepcopy(self.enums_data),
        )
        self._redo_stack.append(current_state)

        # Restore previous state
        self.fields, self.enums_data = self._undo_stack.pop()
        self.modified = True
        self.refresh_fields_table()
        self.refresh_enums_table()
        self.update_status()
        if self.current_view == "field_detail" and self._detail_field_name:
            if not self._populate_detail_view(self._detail_field_name):
                self._switch_to_fields_from_detail()
        elif self.current_view in ("fields", "enums"):
            table = self.query_one(f"#{self.current_view}-table", DataTable)
            self._restore_cursor(table, row_key, row_idx)
        self.notify("Undo")

    def action_redo(self) -> None:
        """Redo the last undone edit."""
        if not self._redo_stack:
            self.notify("Nothing to redo")
            return

        # Remember cursor position in the active table.
        row_key = None
        row_idx = None
        if self.current_view in ("fields", "enums"):
            table = self.query_one(f"#{self.current_view}-table", DataTable)
            row_key = self._get_row_key_at(table, table.cursor_row) if table.cursor_row is not None else None
            row_idx = table.cursor_row

        # Save current state to undo stack
        current_state = (
            copy.deepcopy(self.fields),
            copy.deepcopy(self.enums_data),
        )
        self._undo_stack.append(current_state)

        # Restore redo state
        self.fields, self.enums_data = self._redo_stack.pop()
        self.modified = True
        self.refresh_fields_table()
        self.refresh_enums_table()
        self.update_status()
        if self.current_view == "field_detail" and self._detail_field_name:
            if not self._populate_detail_view(self._detail_field_name):
                self._switch_to_fields_from_detail()
        elif self.current_view in ("fields", "enums"):
            table = self.query_one(f"#{self.current_view}-table", DataTable)
            self._restore_cursor(table, row_key, row_idx)
        self.notify("Redo")

    def action_show_fields(self) -> None:
        """Switch to fields view, restoring last cursor position."""
        if self.current_view == "field_detail":
            self._switch_to_fields_from_detail()
            return
        # Save current enums cursor position
        if self.current_view == "enums":
            enums_table = self.query_one("#enums-table", DataTable)
            if enums_table.cursor_row is not None and enums_table.row_count > 0:
                row_key = self._get_row_key_at(enums_table, enums_table.cursor_row)
                if row_key:
                    self._last_enums_row_key = row_key

        self.current_view = "fields"
        self.query_one("#fields-table").remove_class("hidden")
        self.query_one("#enums-table").add_class("hidden")
        self.query_one("#search-input").remove_class("hidden")

        # Restore last fields cursor position
        if self._last_fields_row_key:
            fields_table = self.query_one("#fields-table", DataTable)
            self._move_cursor_to_key(fields_table, self._last_fields_row_key)

        self.update_status()

    def action_show_enums(self) -> None:
        """Switch to enums view, jumping to field's enum if applicable."""
        target_enum_name: Optional[str] = None

        # If coming from fields view, check if current field has an enum range
        if self.current_view == "fields":
            fields_table = self.query_one("#fields-table", DataTable)
            if fields_table.cursor_row is not None and fields_table.row_count > 0:
                row_key = self._get_row_key_at(fields_table, fields_table.cursor_row)
                if row_key:
                    # Save fields cursor position
                    self._last_fields_row_key = row_key
                    # Find the field and check if its range is an enum
                    field = next((f for f in self.fields if f["name"] == row_key), None)
                    if field:
                        field_range = field.get("range", "")
                        # Check if this range is an enum name
                        enum_names = set(e.get("enum_name", "") for e in self.enums_data)
                        if field_range in enum_names:
                            target_enum_name = field_range

        self.current_view = "enums"
        self.query_one("#fields-table").add_class("hidden")
        self.query_one("#enums-table").remove_class("hidden")
        self.query_one("#search-input").add_class("hidden")

        enums_table = self.query_one("#enums-table", DataTable)
        if target_enum_name:
            # Find first row with this enum name and move cursor there
            for i, enum_row in enumerate(self.enums_data):
                if enum_row.get("enum_name") == target_enum_name:
                    row_key = f"{enum_row.get('enum_name')}_{i}"
                    self._move_cursor_to_key(enums_table, row_key)
                    break
        elif self._last_enums_row_key:
            # Restore last enums cursor position
            self._move_cursor_to_key(enums_table, self._last_enums_row_key)

        self.update_status()

    def _toggle_group(self, group: str) -> None:
        """Toggle collapse/expand for *group* and move cursor to its first row."""
        if not group:
            return
        if group in self.collapsed_groups:
            self.collapsed_groups.discard(group)
        else:
            self.collapsed_groups.add(group)
        table = self.query_one("#fields-table", DataTable)
        saved_scroll_y = table.scroll_y
        self.refresh_fields_table()
        self.update_status()
        # The first field in the group is the row key for both the collapsed
        # summary row and the first expanded row.
        first = next(
            (f for f in self._sorted_fields() if self._get_display_group(f) == group),
            None,
        )
        if first:
            self._move_cursor_to_key(table, first["name"])
        # Restore scroll position after layout so the view doesn't jump.
        self.call_after_refresh(
            lambda: table.scroll_to(y=saved_scroll_y, animate=False)
        )

    def action_toggle_groups(self) -> None:
        """Toggle collapse/expand of current slot group."""
        if self.current_view != "fields":
            return

        table = self.query_one("#fields-table", DataTable)
        if table.cursor_row is None or table.row_count == 0:
            return

        row_key = self._get_row_key_at(table, table.cursor_row)
        if row_key:
            field = next((f for f in self.fields if f["name"] == row_key), None)
            if field:
                self._toggle_group(self._get_display_group(field))

    def action_collapse_all_groups(self) -> None:
        """Collapse every slot group."""
        if self.current_view != "fields":
            return
        all_groups = {self._get_display_group(f) for f in self.fields if self._get_display_group(f)}
        if all_groups == self.collapsed_groups:
            return
        table = self.query_one("#fields-table", DataTable)
        saved_scroll_y = table.scroll_y
        cursor_key = self._get_row_key_at(table, table.cursor_row) if table.cursor_row is not None else None
        self.collapsed_groups = all_groups
        self.refresh_fields_table()
        self.update_status()
        if cursor_key:
            self._move_cursor_to_key(table, cursor_key)
        self.call_after_refresh(
            lambda: table.scroll_to(y=saved_scroll_y, animate=False)
        )

    def action_expand_all_groups(self) -> None:
        """Expand every slot group."""
        if self.current_view != "fields":
            return
        if not self.collapsed_groups:
            return
        table = self.query_one("#fields-table", DataTable)
        saved_scroll_y = table.scroll_y
        cursor_key = self._get_row_key_at(table, table.cursor_row) if table.cursor_row is not None else None
        self.collapsed_groups.clear()
        self.refresh_fields_table()
        self.update_status()
        if cursor_key:
            self._move_cursor_to_key(table, cursor_key)
        self.call_after_refresh(
            lambda: table.scroll_to(y=saved_scroll_y, animate=False)
        )

    def action_toggle_required(self) -> None:
        """Toggle required filter."""
        if self.current_view != "fields":
            return

        self.filter_required = not self.filter_required
        self.refresh_fields_table()
        self.update_status()

    def _swap_rank(self, direction: int) -> None:
        """Swap rank(s) for selected or cursor field(s).

        *direction* is -1 (move up / lower rank) or +1 (move down / higher rank).
        When multiple fields are selected they are processed so that each one
        swaps with the nearest non-selected neighbour in the given direction.
        """
        if self.current_view != "fields":
            return

        table = self.query_one("#fields-table", DataTable)
        if table.cursor_row is None or table.row_count == 0:
            return

        # Determine target fields
        if self._selected_fields:
            target_names = set(self._selected_fields)
        else:
            row_key = self._get_row_key_at(table, table.cursor_row)
            if not row_key:
                return
            target_names = {row_key}

        target_fields = [f for f in self.fields if f["name"] in target_names]
        if not target_fields:
            return

        # Process from the leading edge: ascending rank for up, descending for down.
        target_fields.sort(key=lambda f: f.get("rank", 0), reverse=(direction == 1))

        cursor_key = self._get_row_key_at(table, table.cursor_row)

        self._save_state()
        moved = False

        for field in target_fields:
            current_rank = field.get("rank", 0)
            neighbour = None
            best_rank = None
            for f in self.fields:
                if f["name"] in target_names:
                    continue
                r = f.get("rank", 0)
                if direction == -1 and r < current_rank:
                    if best_rank is None or r > best_rank:
                        neighbour = f
                        best_rank = r
                elif direction == 1 and r > current_rank:
                    if best_rank is None or r < best_rank:
                        neighbour = f
                        best_rank = r

            if neighbour is not None:
                field["rank"] = best_rank
                neighbour["rank"] = current_rank
                moved = True

        if not moved:
            self._undo_stack.pop()
            return

        self.modified = True
        self.refresh_fields_table()
        self.update_status()
        if cursor_key:
            self._move_cursor_to_key(table, cursor_key)

    def action_rank_up(self) -> None:
        """Move the selected field(s) up (decrease rank number)."""
        self._swap_rank(-1)

    def action_rank_down(self) -> None:
        """Move the selected field(s) down (increase rank number)."""
        self._swap_rank(1)

    def _move_rank_to(self, target: str) -> None:
        """Move selected or cursor field(s) to the top or bottom.

        *target* is ``"top"`` or ``"bottom"``.
        """
        if self.current_view != "fields":
            return

        table = self.query_one("#fields-table", DataTable)
        if table.cursor_row is None or table.row_count == 0:
            return

        if self._selected_fields:
            target_names = set(self._selected_fields)
        else:
            row_key = self._get_row_key_at(table, table.cursor_row)
            if not row_key:
                return
            target_names = {row_key}

        target_fields = [f for f in self.fields if f["name"] in target_names]
        if not target_fields:
            return

        cursor_key = self._get_row_key_at(table, table.cursor_row)
        self._save_state()

        if target == "top":
            # Give target fields ranks below the current minimum so they
            # sort first, preserving their relative order.
            target_fields.sort(key=lambda f: f.get("rank", 0))
            min_rank = min(f.get("rank", 0) for f in self.fields)
            for i, f in enumerate(target_fields):
                f["rank"] = min_rank - len(target_fields) + i
        else:
            # Give target fields ranks above the current maximum.
            target_fields.sort(key=lambda f: f.get("rank", 0))
            max_rank = max(f.get("rank", 0) for f in self.fields)
            for i, f in enumerate(target_fields):
                f["rank"] = max_rank + 1 + i

        self._renumber_ranks()
        self.modified = True
        self.refresh_fields_table()
        self.update_status()
        if cursor_key:
            self._move_cursor_to_key(table, cursor_key)

    def action_rank_top(self) -> None:
        """Move the selected field(s) to the top (rank 1)."""
        self._move_rank_to("top")

    def action_rank_bottom(self) -> None:
        """Move the selected field(s) to the bottom (last rank)."""
        self._move_rank_to("bottom")

    # ------------------------------------------------------------------
    # Multi-select helpers
    # ------------------------------------------------------------------

    def _update_selection_indicators(self) -> None:
        """Refresh the fields table to reflect current selection highlighting."""
        if self.current_view != "fields":
            return
        table = self.query_one("#fields-table", DataTable)
        cursor_key = self._get_row_key_at(table, table.cursor_row) if table.cursor_row is not None else None
        cursor_idx = table.cursor_row
        self.refresh_fields_table()
        self._restore_cursor(table, cursor_key, cursor_idx)

    def _update_range_selection(self, table: DataTable) -> None:
        """Select all visible rows between the anchor and the cursor."""
        if self._selection_anchor is None or table.cursor_row is None:
            return

        anchor_idx = None
        for i in range(table.row_count):
            if self._get_row_key_at(table, i) == self._selection_anchor:
                anchor_idx = i
                break
        if anchor_idx is None:
            return

        start = min(anchor_idx, table.cursor_row)
        end = max(anchor_idx, table.cursor_row)

        self._selected_fields.clear()
        for i in range(start, end + 1):
            key = self._get_row_key_at(table, i)
            if key:
                self._selected_fields.add(key)
        self._update_selection_indicators()

    def action_select_extend_up(self) -> None:
        """Extend range selection upward (shift+up)."""
        if self.current_view != "fields":
            return
        table = self.query_one("#fields-table", DataTable)
        if table.row_count == 0 or table.cursor_row is None:
            return
        if self._selection_anchor is None:
            self._selection_anchor = self._get_row_key_at(table, table.cursor_row)
        new_row = max(0, table.cursor_row - 1)
        table.move_cursor(row=new_row)
        self._update_range_selection(table)

    def action_select_extend_down(self) -> None:
        """Extend range selection downward (shift+down)."""
        if self.current_view != "fields":
            return
        table = self.query_one("#fields-table", DataTable)
        if table.row_count == 0 or table.cursor_row is None:
            return
        if self._selection_anchor is None:
            self._selection_anchor = self._get_row_key_at(table, table.cursor_row)
        new_row = min(table.row_count - 1, table.cursor_row + 1)
        table.move_cursor(row=new_row)
        self._update_range_selection(table)

    def action_toggle_select(self) -> None:
        """Toggle selection of current row (space)."""
        if self.current_view != "fields":
            return
        table = self.query_one("#fields-table", DataTable)
        if table.row_count == 0 or table.cursor_row is None:
            return
        current_key = self._get_row_key_at(table, table.cursor_row)
        if current_key:
            self._selected_fields.symmetric_difference_update({current_key})
            self._selection_anchor = None
            self._update_selection_indicators()

    def action_clear_selection(self) -> None:
        """Clear search, selection, or leave detail view."""
        if self.current_view == "field_detail":
            self._switch_to_fields_from_detail()
            return
        # If search input is focused, clear/unfocus it
        try:
            search_input = self.query_one("#search-input", Input)
            if search_input.has_focus:
                if search_input.value:
                    search_input.value = ""
                if self.current_view == "fields":
                    self.query_one("#fields-table", DataTable).focus()
                return
        except Exception:
            pass
        if self._selected_fields:
            self._selected_fields.clear()
            self._selection_anchor = None
            self._update_selection_indicators()

    # ------------------------------------------------------------------
    # Field detail view
    # ------------------------------------------------------------------

    def _populate_detail_view(self, field_name: str) -> bool:
        """Populate detail view widgets from field data. Returns False if not found."""
        field = next((f for f in self.fields if f["name"] == field_name), None)
        if field is None:
            return False
        self._detail_field_name = field_name
        self.query_one("#detail-header", Label).update(f"Field: {field_name}")
        for attr, _label in _DETAIL_ATTRS:
            widget = self.query_one(f"#detail-attr-{attr}", _DetailTextArea)
            if attr == "required":
                value = "Yes" if field.get("required") else "No"
            else:
                value = str(field.get(attr, ""))
            widget.load_text(value)
        return True

    def action_view_field(self) -> None:
        """Open the single field detail view for the cursor field."""
        if self.current_view != "fields":
            return
        table = self.query_one("#fields-table", DataTable)
        if table.cursor_row is None or table.row_count == 0:
            return
        row_key = self._get_row_key_at(table, table.cursor_row)
        if not row_key:
            return
        field = next((f for f in self.fields if f["name"] == row_key), None)
        if not field:
            return
        if self._get_display_group(field) in self.collapsed_groups:
            return
        self._last_fields_row_key = row_key
        if self._populate_detail_view(row_key):
            self.current_view = "field_detail"
            self.query_one("#fields-table").add_class("hidden")
            self.query_one("#field-detail-container").remove_class("hidden")
            self.query_one("#search-input").add_class("hidden")
            self.update_status()
            self.query_one("#detail-attr-name").focus()

    def action_save_field_detail(self) -> None:
        """Save changes from the detail view and return to the fields table."""
        if self.current_view != "field_detail" or not self._detail_field_name:
            return
        field = next((f for f in self.fields if f["name"] == self._detail_field_name), None)
        if not field:
            self._switch_to_fields_from_detail()
            return
        new_values = {}
        for attr, _label in _DETAIL_ATTRS:
            widget = self.query_one(f"#detail-attr-{attr}", _DetailTextArea)
            value = widget.text.strip()
            if attr == "required":
                new_values[attr] = value.lower() in ("yes", "true", "1")
            elif attr == "rank":
                try:
                    new_values[attr] = max(1, int(value))
                except ValueError:
                    new_values[attr] = field.get("rank", 1)
            else:
                new_values[attr] = value
        changed = any(field.get(attr) != new_values[attr] for attr, _ in _DETAIL_ATTRS)
        if changed:
            self._save_state()
            old_rank = field.get("rank", 1)
            for attr, _label in _DETAIL_ATTRS:
                field[attr] = new_values[attr]
            self._detail_field_name = new_values.get("name", self._detail_field_name)
            # If the rank was changed, shift other fields to make room and
            # renumber so that ranks stay sequential with no duplicates.
            new_rank = new_values.get("rank", old_rank)
            if new_rank != old_rank:
                for f in self.fields:
                    if f is field:
                        continue
                    if new_rank <= old_rank and new_rank <= f.get("rank", 0) < old_rank:
                        f["rank"] = f.get("rank", 0) + 1
                    elif new_rank > old_rank and old_rank < f.get("rank", 0) <= new_rank:
                        f["rank"] = f.get("rank", 0) - 1
                self._renumber_ranks()
            self.modified = True
            self.notify("Field updated")
        self._switch_to_fields_from_detail()

    def _switch_to_fields_from_detail(self) -> None:
        """Leave the detail view and return to the fields table."""
        field_name = self._detail_field_name
        self.current_view = "fields"
        self.query_one("#field-detail-container").add_class("hidden")
        self.query_one("#fields-table").remove_class("hidden")
        self.query_one("#search-input").remove_class("hidden")
        self.refresh_fields_table()
        self.update_status()
        if field_name:
            table = self.query_one("#fields-table", DataTable)
            self._move_cursor_to_key(table, field_name)
        self._detail_field_name = None

    @on(Button.Pressed, "#detail-save-btn")
    def _on_detail_save(self) -> None:
        self.action_save_field_detail()

    @on(Button.Pressed, "#detail-discard-btn")
    def _on_detail_discard(self) -> None:
        self._switch_to_fields_from_detail()

    def action_insert_row(self) -> None:
        """Insert a new row."""
        if self.current_view == "fields":
            # Capture the rank of the currently selected row so the new field
            # is inserted at that position.
            insert_rank = None
            table = self.query_one("#fields-table", DataTable)
            if table.cursor_row is not None and table.row_count > 0:
                row_key = self._get_row_key_at(table, table.cursor_row)
                if row_key:
                    cur_field = next((f for f in self.fields if f["name"] == row_key), None)
                    if cur_field:
                        insert_rank = cur_field.get("rank")
            self._insert_rank = insert_rank
            slot_groups = list(set(self._get_display_group(f) for f in self.fields if self._get_display_group(f)))
            self.push_screen(NewFieldScreen(slot_groups), self._on_new_field)
        else:
            enum_names = list(set(e.get("enum_name", "") for e in self.enums_data if e.get("enum_name")))
            self.push_screen(NewEnumValueScreen(enum_names), self._on_new_enum)

    def _on_new_field(self, field: dict) -> None:
        """Handle new field creation."""
        if field:
            self._save_state()
            insert_rank = getattr(self, "_insert_rank", None)
            if insert_rank is not None:
                # Shift existing fields at or after the insertion point down.
                for f in self.fields:
                    if f.get("rank", 0) >= insert_rank:
                        f["rank"] = f.get("rank", 0) + 1
                field["rank"] = insert_rank
            else:
                # No cursor context – append after the last field.
                max_rank = max((f.get("rank", 0) for f in self.fields), default=0)
                field["rank"] = max_rank + 1
            self.fields.append(field)
            self._renumber_ranks()
            self.modified = True
            self.refresh_fields_table()
            self.update_status()
            self.notify(f"Added field: {field['name']}")

    def _on_new_enum(self, enum_row: dict) -> None:
        """Handle new enum value creation."""
        if enum_row:
            self._save_state()
            self.enums_data.append(enum_row)
            self.modified = True
            self.refresh_enums_table()
            self.update_status()
            self.notify(f"Added enum value: {enum_row['value']}")

    def action_delete_row(self) -> None:
        """Delete selected fields (or cursor row).

        Deletes immediately unless any target field is required, in which
        case a confirmation prompt warns the user first.
        """
        if self.current_view == "fields":
            table = self.query_one("#fields-table", DataTable)
            if table.cursor_row is None or table.row_count == 0:
                return

            # Determine targets
            if self._selected_fields:
                target_names = set(self._selected_fields)
            else:
                row_key = self._get_row_key_at(table, table.cursor_row)
                if not row_key:
                    return
                target_names = {row_key}

            # Check for required fields among targets
            required_names = sorted(
                name for name in target_names
                if any(f["name"] == name and f.get("required") for f in self.fields)
            )

            if required_names:
                if len(target_names) == 1:
                    msg = f"'{required_names[0]}' is a required field. Delete anyway?"
                else:
                    msg = (
                        f"{len(required_names)} of {len(target_names)} selected "
                        f"fields are required. Delete anyway?"
                    )
                self.push_screen(ConfirmScreen(msg), self._on_delete_confirm)
                return

            self._do_delete()
        else:
            table = self.query_one("#enums-table", DataTable)
            if table.cursor_row is None or table.row_count == 0:
                return
            self._do_delete()

    def _on_delete_confirm(self, confirmed: bool) -> None:
        """Handle delete confirmation for required fields."""
        if confirmed:
            self._do_delete()

    def _do_delete(self) -> None:
        """Perform the row deletion and keep cursor at the same position."""
        if self.current_view == "fields":
            table = self.query_one("#fields-table", DataTable)
            if table.cursor_row is not None and table.row_count > 0:
                cursor_pos = table.cursor_row

                if self._selected_fields:
                    target_names = set(self._selected_fields)
                else:
                    row_key = self._get_row_key_at(table, cursor_pos)
                    if not row_key:
                        return
                    target_names = {row_key}

                self._save_state()
                deleted_count = len(target_names)
                self.fields = [f for f in self.fields if f["name"] not in target_names]
                self._renumber_ranks()
                self._selected_fields.clear()
                self._selection_anchor = None
                self.modified = True
                self.refresh_fields_table()
                self.update_status()
                if table.row_count > 0:
                    table.move_cursor(row=min(cursor_pos, table.row_count - 1))
                self.notify(f"Deleted {deleted_count} field(s)")
        else:
            table = self.query_one("#enums-table", DataTable)
            if table.cursor_row is not None and table.row_count > 0:
                cursor_pos = table.cursor_row
                row_key = self._get_row_key_at(table, cursor_pos)
                if row_key:
                    # Parse the key to find the enum row (format: "EnumName_index")
                    parts = row_key.rsplit("_", 1)
                    if len(parts) == 2:
                        try:
                            idx = int(parts[1])
                            if 0 <= idx < len(self.enums_data):
                                self._save_state()
                                removed = self.enums_data.pop(idx)
                                self.modified = True
                                self.refresh_enums_table()
                                self.update_status()
                                if table.row_count > 0:
                                    table.move_cursor(row=min(cursor_pos, table.row_count - 1))
                                self.notify(f"Deleted enum value: {removed.get('value', '')}")
                        except (ValueError, IndexError):
                            pass

    def action_edit_cell(self) -> None:
        """Edit the selected cell."""
        if self.current_view == "field_detail":
            return
        if self.current_view == "fields":
            table = self.query_one("#fields-table", DataTable)
            columns = ["_grp", "rank", "slot_group", "source", "required", "name", "title", "description", "range", "pattern", "comments"]
        else:
            table = self.query_one("#enums-table", DataTable)
            columns = ["enum_name", "value", "text", "description"]

        if table.cursor_row is None or table.cursor_column is None or table.row_count == 0:
            return

        col_idx = table.cursor_column
        if col_idx >= len(columns):
            return

        column = columns[col_idx]
        row_key = self._get_row_key_at(table, table.cursor_row)

        if not row_key:
            return

        # Fields-specific: handle group toggle column and collapsed rows.
        if self.current_view == "fields":
            field = next((f for f in self.fields if f["name"] == row_key), None)
            if not field:
                return
            group = self._get_display_group(field)
            if column == "_grp":
                if group:
                    self._toggle_group(group)
                return
            if group in self.collapsed_groups:
                return  # collapsed summary row is not editable
            current_value = str(field.get(column, ""))
            self.push_screen(
                EditCellScreen(column, current_value),
                lambda v, rk=row_key: self._on_field_edit(rk, column, v)
            )
            return

        # Enums branch
        # Parse the key to find the enum row (format: "EnumName_index")
        parts = row_key.rsplit("_", 1)
        if len(parts) == 2:
            try:
                idx = int(parts[1])
                if 0 <= idx < len(self.enums_data):
                    enum_row = self.enums_data[idx]
                    current_value = str(enum_row.get(column, ""))
                    self.push_screen(
                        EditCellScreen(column, current_value),
                        lambda v, i=idx: self._on_enum_edit(i, column, v)
                    )
            except (ValueError, IndexError):
                pass

    def _on_field_edit(self, field_name: str, column: str, value: str) -> None:
        """Handle field edit."""
        for field in self.fields:
            if field["name"] == field_name:
                # Check if value actually changed
                old_value = field.get(column, "")
                if column == "required":
                    new_value = value.lower() in ("yes", "true", "1")
                elif column == "rank":
                    try:
                        new_value = int(value)
                    except ValueError:
                        return
                else:
                    new_value = value
                if old_value != new_value:
                    self._save_state()
                    field[column] = new_value
                    self.modified = True
                    self.refresh_fields_table()
                    self.update_status()
                break

    def _on_enum_edit(self, idx: int, column: str, value: str) -> None:
        """Handle enum edit."""
        if 0 <= idx < len(self.enums_data):
            # Check if value actually changed
            old_value = self.enums_data[idx].get(column, "")
            if old_value != value:
                self._save_state()
                self.enums_data[idx][column] = value
                self.modified = True
                self.refresh_enums_table()
                self.update_status()

    def action_save_schema(self) -> None:
        """Save/export the schema."""
        default_name = os.path.basename(self.current_file) if self.current_file else "schema.yaml"
        if not default_name.endswith(".yaml"):
            default_name += ".yaml"

        self.push_screen(SaveScreen(default_name), self._on_save)

    def _on_save(self, filename: str) -> None:
        """Handle save filename."""
        if not filename:
            return

        if not filename.endswith((".yaml", ".yml")):
            filename += ".yaml"

        # Prepend schemas/ if no directory specified
        if os.path.dirname(filename) == "":
            filename = os.path.join("schemas", filename)

        try:
            # Sort fields by rank so that reordering is preserved in the
            # saved file.  rebuild_schema assigns sequential ranks based on
            # list position, so the order must reflect the user's intent.
            ordered_fields = sorted(self.fields, key=lambda f: f.get("rank", 0))
            new_schema = rebuild_schema(self.schema, ordered_fields, self.enums_data)
            save_schema(new_schema, filename)
            self.current_file = filename
            self.schema = new_schema
            # Re-extract fields so self.fields reflects the saved ranks.
            self.fields = extract_fields(new_schema)
            self.modified = False
            self.update_status()
            self.notify(f"Saved: {filename}")
        except Exception as e:
            self.notify(f"Error saving: {e}", severity="error")

    def action_open_schema(self) -> None:
        """Open a schema file."""
        self.push_screen(FileSelectScreen(), self._on_file_selected)

    def _on_file_selected(self, filepath: str) -> None:
        """Handle file selection."""
        if filepath:
            self.load_file(filepath)

    def action_quit_app(self) -> None:
        """Quit the application."""
        if self.modified:
            self.push_screen(
                ConfirmScreen("Unsaved changes. Quit anyway?"),
                self._on_quit_confirm
            )
        else:
            self._cleanup_elasticsearch()
            self.exit()

    def _on_quit_confirm(self, confirmed: bool) -> None:
        """Handle quit confirmation."""
        if confirmed:
            self._cleanup_elasticsearch()
            self.exit()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    """Main entry point."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Interactive LinkML schema editor for DataHarmonizer",
    )
    parser.add_argument(
        "file",
        nargs="?",
        help="LinkML schema file to open",
    )
    parser.add_argument(
        "--es-url",
        default=None,
        help="Elasticsearch server URL (default: http://localhost:9200)",
    )
    parser.add_argument(
        "--log",
        default=None,
        metavar="PATH",
        help="Write log output to PATH",
    )
    args = parser.parse_args()

    if args.log:
        setup_logging(args.log)

    app = LinkMLEditor(initial_file=args.file, es_url=args.es_url)
    app.run()


if __name__ == "__main__":
    main()
