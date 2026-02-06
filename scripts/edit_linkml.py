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
    r - Toggle required filter
    i - Insert new row
    d - Delete selected row
    Enter - Edit selected cell
    s - Save/Export schema
    o - Open schema file
    q - Quit
"""

import os
import sys
from pathlib import Path
from typing import Optional

import yaml

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
    """

    BINDINGS = [
        Binding("f", "show_fields", "Fields"),
        Binding("e", "show_enums", "Enums"),
        Binding("g", "toggle_groups", "Toggle Groups"),
        Binding("r", "toggle_required", "Filter Required"),
        Binding("i", "insert_row", "Insert"),
        Binding("d", "delete_row", "Delete"),
        Binding("enter", "edit_cell", "Edit"),
        Binding("s", "save_schema", "Save"),
        Binding("o", "open_schema", "Open"),
        Binding("q", "quit_app", "Quit"),
    ]

    def __init__(self, initial_file: Optional[str] = None):
        super().__init__()
        self.schema: dict = {}
        self.fields: list[dict] = []
        self.enums_data: list[dict] = []
        self.current_file: str = ""
        self.initial_file = initial_file
        self.current_view = "fields"  # "fields" or "enums"
        self.filter_required = False
        self.collapsed_groups: set[str] = set()
        self.modified = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield Container(
            Horizontal(
                Label("File: None", id="file-label"),
                Label("View: Fields", id="view-label"),
                Label("", id="filter-label"),
                id="status-bar",
            ),
            VerticalScroll(
                DataTable(id="fields-table"),
                DataTable(id="enums-table", classes="hidden"),
                id="table-container",
            ),
            id="main-container",
        )
        yield Footer()

    def on_mount(self) -> None:
        # Setup fields table
        fields_table = self.query_one("#fields-table", DataTable)
        fields_table.cursor_type = "row"
        fields_table.add_columns(
            "slot_group", "required", "name", "title", "description", "range", "pattern", "comments"
        )

        # Setup enums table
        enums_table = self.query_one("#enums-table", DataTable)
        enums_table.cursor_type = "row"
        enums_table.add_columns("enum_name", "value", "text", "description")

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

            self.refresh_fields_table()
            self.refresh_enums_table()
            self.update_status()

            self.notify(f"Loaded: {filepath}")
        except Exception as e:
            self.notify(f"Error loading file: {e}", severity="error")

    def refresh_fields_table(self) -> None:
        """Refresh the fields table with current data."""
        table = self.query_one("#fields-table", DataTable)
        table.clear()

        for field in self.fields:
            # Apply filters
            if self.filter_required and not field.get("required"):
                continue

            group = field.get("slot_group", "")
            if group in self.collapsed_groups:
                # Show only first field of collapsed group
                first_in_group = next(
                    (f for f in self.fields if f.get("slot_group") == group),
                    None
                )
                if first_in_group and first_in_group["name"] != field["name"]:
                    continue

            table.add_row(
                field.get("slot_group", ""),
                "Yes" if field.get("required") else "No",
                field.get("name", ""),
                field.get("title", ""),
                self._truncate(field.get("description", ""), 50),
                field.get("range", "string"),
                field.get("pattern", ""),
                self._truncate(field.get("comments", ""), 30),
                key=field.get("name"),
            )

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

    def update_status(self) -> None:
        """Update status bar labels."""
        file_label = self.query_one("#file-label", Label)
        view_label = self.query_one("#view-label", Label)
        filter_label = self.query_one("#filter-label", Label)

        filename = os.path.basename(self.current_file) if self.current_file else "None"
        modified = " *" if self.modified else ""
        file_label.update(f"File: {filename}{modified}")

        view_label.update(f"View: {self.current_view.title()}")

        filters = []
        if self.filter_required:
            filters.append("Required only")
        if self.collapsed_groups:
            filters.append(f"{len(self.collapsed_groups)} groups collapsed")
        filter_label.update(" | ".join(filters))

    def action_show_fields(self) -> None:
        """Switch to fields view."""
        self.current_view = "fields"
        self.query_one("#fields-table").remove_class("hidden")
        self.query_one("#enums-table").add_class("hidden")
        self.update_status()

    def action_show_enums(self) -> None:
        """Switch to enums view."""
        self.current_view = "enums"
        self.query_one("#fields-table").add_class("hidden")
        self.query_one("#enums-table").remove_class("hidden")
        self.update_status()

    def action_toggle_groups(self) -> None:
        """Toggle collapse/expand of current slot group."""
        if self.current_view != "fields":
            return

        table = self.query_one("#fields-table", DataTable)
        if table.cursor_row is None:
            return

        row_key = table.get_row_at(table.cursor_row)
        if row_key:
            # Find the field by name (first column with actual name is index 2)
            row_data = table.get_row(row_key)
            if row_data:
                group = row_data[0]  # slot_group is first column
                if group:
                    if group in self.collapsed_groups:
                        self.collapsed_groups.discard(group)
                    else:
                        self.collapsed_groups.add(group)
                    self.refresh_fields_table()
                    self.update_status()

    def action_toggle_required(self) -> None:
        """Toggle required filter."""
        if self.current_view != "fields":
            return

        self.filter_required = not self.filter_required
        self.refresh_fields_table()
        self.update_status()

    def action_insert_row(self) -> None:
        """Insert a new row."""
        if self.current_view == "fields":
            slot_groups = list(set(f.get("slot_group", "") for f in self.fields if f.get("slot_group")))
            self.push_screen(NewFieldScreen(slot_groups), self._on_new_field)
        else:
            enum_names = list(set(e.get("enum_name", "") for e in self.enums_data if e.get("enum_name")))
            self.push_screen(NewEnumValueScreen(enum_names), self._on_new_enum)

    def _on_new_field(self, field: dict) -> None:
        """Handle new field creation."""
        if field:
            self.fields.append(field)
            self.modified = True
            self.refresh_fields_table()
            self.update_status()
            self.notify(f"Added field: {field['name']}")

    def _on_new_enum(self, enum_row: dict) -> None:
        """Handle new enum value creation."""
        if enum_row:
            self.enums_data.append(enum_row)
            self.modified = True
            self.refresh_enums_table()
            self.update_status()
            self.notify(f"Added enum value: {enum_row['value']}")

    def action_delete_row(self) -> None:
        """Delete the selected row."""
        if self.current_view == "fields":
            table = self.query_one("#fields-table", DataTable)
        else:
            table = self.query_one("#enums-table", DataTable)

        if table.cursor_row is None or table.row_count == 0:
            return

        self.push_screen(
            ConfirmScreen("Delete this row?"),
            self._on_delete_confirm
        )

    def _on_delete_confirm(self, confirmed: bool) -> None:
        """Handle delete confirmation."""
        if not confirmed:
            return

        if self.current_view == "fields":
            table = self.query_one("#fields-table", DataTable)
            if table.cursor_row is not None:
                row_key = table.get_row_at(table.cursor_row)
                if row_key:
                    # Find and remove field by name
                    field_name = str(row_key)
                    self.fields = [f for f in self.fields if f["name"] != field_name]
                    self.modified = True
                    self.refresh_fields_table()
                    self.update_status()
                    self.notify(f"Deleted field: {field_name}")
        else:
            table = self.query_one("#enums-table", DataTable)
            if table.cursor_row is not None:
                row_key = table.get_row_at(table.cursor_row)
                if row_key:
                    # Parse the key to find the enum row
                    key_str = str(row_key)
                    parts = key_str.rsplit("_", 1)
                    if len(parts) == 2:
                        try:
                            idx = int(parts[1])
                            if 0 <= idx < len(self.enums_data):
                                removed = self.enums_data.pop(idx)
                                self.modified = True
                                self.refresh_enums_table()
                                self.update_status()
                                self.notify(f"Deleted enum value: {removed.get('value', '')}")
                        except (ValueError, IndexError):
                            pass

    def action_edit_cell(self) -> None:
        """Edit the selected cell."""
        if self.current_view == "fields":
            table = self.query_one("#fields-table", DataTable)
            columns = ["slot_group", "required", "name", "title", "description", "range", "pattern", "comments"]
        else:
            table = self.query_one("#enums-table", DataTable)
            columns = ["enum_name", "value", "text", "description"]

        if table.cursor_row is None or table.cursor_column is None:
            return

        col_idx = table.cursor_column
        if col_idx >= len(columns):
            return

        column = columns[col_idx]
        row_key = table.get_row_at(table.cursor_row)

        if not row_key:
            return

        # Get current value
        if self.current_view == "fields":
            field = next((f for f in self.fields if f["name"] == str(row_key)), None)
            if field:
                current_value = str(field.get(column, ""))
                self.push_screen(
                    EditCellScreen(column, current_value),
                    lambda v: self._on_field_edit(str(row_key), column, v)
                )
        else:
            key_str = str(row_key)
            parts = key_str.rsplit("_", 1)
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
                if column == "required":
                    field[column] = value.lower() in ("yes", "true", "1")
                else:
                    field[column] = value
                self.modified = True
                self.refresh_fields_table()
                self.update_status()
                break

    def _on_enum_edit(self, idx: int, column: str, value: str) -> None:
        """Handle enum edit."""
        if 0 <= idx < len(self.enums_data):
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
            new_schema = rebuild_schema(self.schema, self.fields, self.enums_data)
            save_schema(new_schema, filename)
            self.current_file = filename
            self.schema = new_schema
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
            self.exit()

    def _on_quit_confirm(self, confirmed: bool) -> None:
        """Handle quit confirmation."""
        if confirmed:
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
    args = parser.parse_args()

    app = LinkMLEditor(initial_file=args.file)
    app.run()


if __name__ == "__main__":
    main()
