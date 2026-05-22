"""DataHarmonizer JSON data operations driven by a LinkML schema.

Public functions:
    filter_columns(data, schema, where) -> dict           # SQL WHERE on slot metadata
    remap_titles_to_names(records, schema) -> records     # DH exports use slot titles
    validate(records, schema, **opts) -> (is_valid, msgs)

The SQL WHERE in ``filter_columns`` operates on an in-memory SQLite table
``slots`` with columns (name, title, source, required INTEGER 0/1, slot_group,
rank, range). Examples:

    filter_columns(data, schema, "source = 'ENA.sample' OR required = 1")
    filter_columns(data, schema, "name IN ('alias', 'TAXON_ID')")
    filter_columns(data, schema, "source LIKE 'ERC%'")
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from typing import Any

from .schema import get_main_class, slot_meta, title_to_slot_map


_BOOL_STRINGS = frozenset({"true", "false", "yes", "no"})


# ---------------------------------------------------------------------------
# Column filtering via SQL WHERE on slot metadata
# ---------------------------------------------------------------------------

def filter_columns(data: dict[str, Any], schema: dict[str, Any], where: str) -> dict[str, Any]:
    """Filter columns of a DataHarmonizer JSON export by SQL WHERE on slot metadata.

    Returns a new ``data`` dict preserving the original ``Container`` structure.
    Raises ValueError for invalid WHERE clauses or malformed DH JSON.
    """
    container = data.get("Container", data)
    if not isinstance(container, dict):
        raise ValueError("Expected JSON with a 'Container' object at the top level")
    container_key = next(iter(container))
    records = container[container_key]
    if not isinstance(records, list):
        raise ValueError(f"Container.{container_key!r} is not a list of records")

    rows = slot_meta(schema)
    selected_names = _select_slot_names(rows, where)
    t2n = title_to_slot_map(schema)
    name_to_title = {v: k for k, v in t2n.items()}
    keep_titles = {name_to_title.get(n, n) for n in selected_names}

    filtered = [{k: v for k, v in row.items() if k in keep_titles} for row in records]
    return {**data, "Container": {container_key: filtered}}


def _select_slot_names(rows: list[dict[str, Any]], where: str) -> set[str]:
    con = sqlite3.connect(":memory:")
    con.execute(
        "CREATE TABLE slots "
        "(name TEXT, title TEXT, source TEXT, required INTEGER, "
        " slot_group TEXT, rank INTEGER, range TEXT)"
    )
    con.executemany("INSERT INTO slots VALUES (?,?,?,?,?,?,?)", [
        (r["name"], r["title"], r["source"], int(r["required"]),
         r["slot_group"], r["rank"], r["range"])
        for r in rows
    ])
    try:
        result = con.execute(f"SELECT name FROM slots WHERE {where}").fetchall()
    except sqlite3.OperationalError as exc:
        raise ValueError(f"Invalid SQL WHERE clause: {exc}") from exc
    return {row[0] for row in result}


# ---------------------------------------------------------------------------
# Title → name remapping
# ---------------------------------------------------------------------------

def remap_titles_to_names(
    records: list[dict[str, Any]],
    schema: dict[str, Any],
) -> list[dict[str, Any]]:
    """Remap record keys from slot titles (DataHarmonizer exports) to slot names."""
    t2n = title_to_slot_map(schema)
    if not t2n:
        return records
    return [{t2n.get(k, k): v for k, v in record.items()} for record in records]


# ---------------------------------------------------------------------------
# Record validation against the schema
# ---------------------------------------------------------------------------

def validate(
    records: Sequence[dict[str, Any]],
    schema: dict[str, Any],
    *,
    label_fields: Sequence[str] = ("alias",),
    entity_name: str = "record",
    unknown_field_note: str = "will be ignored",
) -> tuple[bool, list[str]]:
    """Validate records against a LinkML schema (required fields, enums, types).

    Returns (is_valid, messages). Messages are human-readable INFO/WARNING/ERROR
    lines suitable for logging.
    """
    messages: list[str] = []
    slots = schema.get("slots") or {}
    enums = schema.get("enums") or {}

    _, main_class = get_main_class(schema)
    if main_class is None:
        messages.append("ERROR: No class with is_a: dh_interface found in LinkML schema")
        return False, messages

    class_slot_names = main_class.get("slots") or []
    messages.append(f"LinkML schema defines {len(class_slot_names)} slots: {', '.join(class_slot_names)}")

    required_slots = {n for n in class_slot_names if (slots.get(n) or {}).get("required")}
    slot_ranges = {n: (slots.get(n) or {}).get("range", "string") for n in class_slot_names}

    messages.append("Required slots: " + ", ".join(sorted(required_slots)))

    is_valid = True
    for i, record in enumerate(records):
        label = _record_label(record, label_fields, entity_name, i)
        messages.append(f"\n--- Validating {entity_name}: {label} ---")

        for key in record:
            if key not in class_slot_names and key != "alias":
                messages.append(f"  WARNING: Unknown field '{key}' not in LinkML schema ({unknown_field_note})")

        for req in required_slots:
            val = record.get(req)
            if val is None or (isinstance(val, str) and not val.strip()):
                messages.append(f"  ERROR: Required field '{req}' is missing or empty")
                is_valid = False
            else:
                messages.append(f"  OK: Required field '{req}' = {val!r}")

        for slot_name in class_slot_names:
            val = record.get(slot_name)
            if val is None:
                continue
            expected_range = slot_ranges.get(slot_name, "string")
            enum_def = enums.get(expected_range)
            if enum_def:
                valid, msg = _check_enum(slot_name, val, enum_def)
            elif expected_range == "boolean":
                valid, msg = _check_boolean(slot_name, val)
            elif expected_range == "integer":
                valid, msg = _check_integer(slot_name, val)
            else:
                messages.append(f"  OK: Field '{slot_name}' = {val!r} (string)")
                continue
            messages.append(msg)
            if not valid:
                is_valid = False

    return is_valid, messages


def _record_label(record, label_fields, entity_name, index):
    for field in label_fields:
        val = record.get(field)
        if val:
            return str(val)
    return f"{entity_name}[{index}]"


def _check_enum(slot_name, val, enum_def):
    allowed = list((enum_def.get("permissible_values") or {}).keys())
    if val not in allowed:
        return False, f"  ERROR: Field '{slot_name}' value {val!r} not in allowed values: {allowed}"
    return True, f"  OK: Field '{slot_name}' = {val!r} (valid enum)"


def _check_boolean(slot_name, val):
    if isinstance(val, bool) or (isinstance(val, str) and val.lower() in _BOOL_STRINGS):
        return True, f"  OK: Field '{slot_name}' = {val!r} (boolean)"
    return False, f"  ERROR: Field '{slot_name}' should be boolean, got {val!r}"


def _check_integer(slot_name, val):
    try:
        int(val)
    except (ValueError, TypeError):
        return False, f"  ERROR: Field '{slot_name}' should be integer, got {val!r}"
    return True, f"  OK: Field '{slot_name}' = {val!r} (integer)"
