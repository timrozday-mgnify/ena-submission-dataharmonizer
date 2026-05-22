"""Schema introspection: read-only helpers that derive info from a LinkML dict.

Public functions:
    get_main_class(schema) -> (name, dict) | (None, None)
    ordered_slot_names(schema) -> list[str]
    slot_to_title_map(schema) -> dict[str, str]
    title_to_slot_map(schema) -> dict[str, str]
    referenced_enums(slots_dict) -> set[str]
    slot_meta(schema) -> list[dict]
    summary(schema) -> dict
    diff(a, b) -> dict
"""

from __future__ import annotations

from collections import Counter
from typing import Any


# Columns produced by slot_meta() — used by both the library and the CLI.
SLOT_META_COLUMNS = ("name", "title", "source", "required", "slot_group", "rank", "range")


def get_main_class(schema: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
    """Return (name, class_dict) for the main class (is_a: dh_interface)."""
    for name, cls in schema.get("classes", {}).items():
        if isinstance(cls, dict) and cls.get("is_a") == "dh_interface":
            return name, cls
    return None, None


def ordered_slot_names(schema: dict[str, Any]) -> list[str]:
    """Return the slot names listed in the main class, preserving order."""
    _, main_cls = get_main_class(schema)
    return list(main_cls.get("slots", [])) if main_cls else []


def slot_to_title_map(schema: dict[str, Any]) -> dict[str, str]:
    """Return a mapping of slot name → slot title for slots that have a title."""
    return {
        name: (defn or {}).get("title", "")
        for name, defn in schema.get("slots", {}).items()
        if (defn or {}).get("title")
    }


def title_to_slot_map(schema: dict[str, Any]) -> dict[str, str]:
    """Return a mapping of slot title → slot name for slots that have a title."""
    return {
        (defn or {}).get("title", ""): name
        for name, defn in schema.get("slots", {}).items()
        if (defn or {}).get("title")
    }


def referenced_enums(slots_dict: dict[str, Any]) -> set[str]:
    """Return the set of enum names referenced by any slot's ``range``."""
    return {
        defn.get("range", "")
        for defn in slots_dict.values()
        if defn.get("range")
    }


def slot_meta(schema: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten per-slot metadata from a schema into a list of rows.

    Each row has keys: name, title, source, required (bool), slot_group, rank, range.
    Rows are returned in the order of the main class's ``slots`` list.
    """
    _, main_cls = get_main_class(schema)
    if main_cls is None:
        return []
    slot_usage = main_cls.get("slot_usage") or {}
    slots = schema.get("slots") or {}
    result = []
    for slot_name in main_cls.get("slots") or []:
        slot = slots.get(slot_name) or {}
        usage = slot_usage.get(slot_name) or {}
        result.append({
            "name": slot_name,
            "title": slot.get("title", slot_name),
            "source": (slot.get("annotations") or {}).get("source", ""),
            "required": bool(slot.get("required", False)),
            "slot_group": usage.get("slot_group", ""),
            "rank": usage.get("rank", 9999),
            "range": slot.get("range", "string"),
        })
    return result


def summary(schema: dict[str, Any]) -> dict[str, Any]:
    """Return a summary of slot counts grouped by source, slot_group, and required."""
    rows = slot_meta(schema)
    return {
        "total_slots": len(rows),
        "required_slots": sum(1 for r in rows if r["required"]),
        "by_source": dict(Counter(r["source"] for r in rows)),
        "by_slot_group": dict(Counter(r["slot_group"] for r in rows)),
        "by_range": dict(Counter(r["range"] for r in rows)),
        "total_enums": len(schema.get("enums") or {}),
    }


def diff(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Compare two schemas. Returns dict with 'added', 'removed', 'changed'.

    'changed' lists slots where title/range/required differs between a and b.
    """
    a_slots = a.get("slots") or {}
    b_slots = b.get("slots") or {}
    a_names = set(a_slots.keys())
    b_names = set(b_slots.keys())

    fields_to_compare = ("title", "range", "required")
    changed: list[dict[str, Any]] = []
    for name in sorted(a_names & b_names):
        a_def = a_slots[name] or {}
        b_def = b_slots[name] or {}
        deltas = {
            f: (a_def.get(f), b_def.get(f))
            for f in fields_to_compare
            if a_def.get(f) != b_def.get(f)
        }
        if deltas:
            changed.append({"name": name, "changes": deltas})

    return {
        "added": sorted(b_names - a_names),
        "removed": sorted(a_names - b_names),
        "changed": changed,
    }
