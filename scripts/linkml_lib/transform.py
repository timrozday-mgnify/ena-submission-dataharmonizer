"""Schema transformations: merge and filter LinkML schemas.

Public functions:
    merge(schemas, *, source_names=None, name=None, title=None,
          description=None, base_uri=None) -> dict
    filter(schema, *, include=None, exclude=None) -> dict
    load_field_list(path) -> list[str]   # newline-separated names file
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Sequence

from .schema import get_main_class, ordered_slot_names, referenced_enums


def load_field_list(path: str | Path) -> list[str]:
    """Read a newline-separated text file of field names (# comments and blanks skipped)."""
    with open(path, "r", encoding="utf-8") as fh:
        return [line.strip() for line in fh
                if line.strip() and not line.strip().startswith("#")]


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------

def merge(
    schemas: Sequence[dict[str, Any]],
    *,
    source_names: Sequence[str] | None = None,
    name: str | None = None,
    title: str | None = None,
    description: str | None = None,
    base_uri: str | None = None,
) -> dict[str, Any]:
    """Merge multiple LinkML schemas. Highest priority is first.

    When a slot/enum/slot_usage entry appears in more than one schema, the
    highest-priority definition wins. Slots from lower-priority schemas are
    appended in their original order. Ranks are renumbered 1..N.

    Defaults for name/title/description/base_uri come from the first schema.
    """
    if not schemas:
        return {}

    merged_slots: dict[str, Any] = {}
    merged_enums: dict[str, Any] = {}
    merged_slot_usage: dict[str, Any] = {}
    seen_slot_order: list[str] = []

    for idx, schema in enumerate(schemas):
        source_prefix = source_names[idx] if source_names and idx < len(source_names) else None
        _, main_cls = get_main_class(schema)
        slots = schema.get("slots") or {}
        slot_usage = (main_cls or {}).get("slot_usage") or {}

        for slot_name in ordered_slot_names(schema):
            if slot_name not in merged_slots:
                seen_slot_order.append(slot_name)
                if slot_name in slots:
                    slot_def = dict(slots[slot_name])
                    if source_prefix:
                        slot_def["source"] = source_prefix
                    merged_slots[slot_name] = slot_def
            if slot_name not in merged_slot_usage and slot_name in slot_usage:
                merged_slot_usage[slot_name] = dict(slot_usage[slot_name])

        for enum_name, enum_def in (schema.get("enums") or {}).items():
            merged_enums.setdefault(enum_name, enum_def)

    # Pick up slots present in `slots` but not in the main class's slot list.
    for idx, schema in enumerate(schemas):
        source_prefix = source_names[idx] if source_names and idx < len(source_names) else None
        for slot_name, slot_def in (schema.get("slots") or {}).items():
            if slot_name in merged_slots:
                continue
            slot_def = dict(slot_def)
            if source_prefix:
                slot_def["source"] = source_prefix
            merged_slots[slot_name] = slot_def
            seen_slot_order.append(slot_name)

    renumbered_usage = {
        slot_name: {**merged_slot_usage.get(slot_name, {}), "rank": rank}
        for rank, slot_name in enumerate(seen_slot_order, start=1)
    }

    return _assemble_merged(
        seen_slot_order=seen_slot_order,
        merged_slots=merged_slots,
        merged_enums=merged_enums,
        merged_slot_usage=renumbered_usage,
        schemas=schemas,
        name=name, title=title, description=description, base_uri=base_uri,
    )


def _assemble_merged(
    *, seen_slot_order, merged_slots, merged_enums, merged_slot_usage,
    schemas, name, title, description, base_uri,
):
    first = schemas[0]
    name = name or first.get("name", "merged")
    title = title or first.get("title", name)
    description = description if description is not None else first.get("description", "")
    if base_uri is None:
        first_id = first.get("id", "")
        base_uri = first_id.rsplit("/", 1)[0] if "/" in first_id else first_id
    schema_id = base_uri.rstrip("/") + "/" + name

    merged_prefixes: dict[str, Any] = {}
    for s in reversed(schemas):
        merged_prefixes.update(s.get("prefixes") or {})

    main_class = {
        "name": name, "title": title, "description": description, "is_a": "dh_interface",
        "slots": list(seen_slot_order), "slot_usage": merged_slot_usage,
    }

    schema: dict[str, Any] = {
        "id": schema_id,
        "name": name,
        "title": title,
        "description": description,
        "version": first.get("version", "1.0.0"),
        "imports": first.get("imports", ["linkml:types"]),
        "prefixes": merged_prefixes,
        "default_range": first.get("default_range", "string"),
        "classes": {
            "dh_interface": {
                "name": "dh_interface",
                "description": "A DataHarmonizer interface",
                "from_schema": schema_id,
            },
            name: main_class,
        },
        "slots": merged_slots,
    }
    if merged_enums:
        schema["enums"] = merged_enums
    return schema


# ---------------------------------------------------------------------------
# Filter
# ---------------------------------------------------------------------------

def filter(  # noqa: A001 — intentional shadow of builtin in module API
    schema: dict[str, Any],
    *,
    include: Sequence[str] | None = None,
    exclude: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Filter slots in a schema. Always prunes unreferenced enums and renumbers ranks.

    - include=None → start from all slots; include=[a,b] → keep only a,b (preserving order).
    - exclude=[x]  → drop x from whatever set is kept.
    Both can be combined: include first, then exclude.
    """
    main_name, main_cls = get_main_class(schema)
    if main_cls is None:
        print("Warning: no main class (is_a: dh_interface) found in schema", file=sys.stderr)
        return schema

    all_slot_names = list(main_cls.get("slots") or [])
    all_slot_set = set(all_slot_names)
    _warn_unknown("include", include, all_slot_set)
    _warn_unknown("exclude", exclude, all_slot_set)

    if include is not None:
        include_set = set(include)
        kept = [s for s in all_slot_names if s in include_set]
    else:
        kept = list(all_slot_names)
    if exclude is not None:
        exclude_set = set(exclude)
        kept = [s for s in kept if s not in exclude_set]

    old_slot_usage = main_cls.get("slot_usage") or {}
    new_slot_usage = {
        slot_name: {**old_slot_usage.get(slot_name, {}), "rank": rank}
        for rank, slot_name in enumerate(kept, start=1)
    }

    new_main_cls = {**main_cls, "slots": list(kept), "slot_usage": new_slot_usage}

    old_slots = schema.get("slots") or {}
    new_slots = {name: old_slots[name] for name in kept if name in old_slots}

    out: dict[str, Any] = {
        k: v for k, v in schema.items() if k not in ("classes", "slots", "enums")
    }
    out["classes"] = {
        cls_name: (new_main_cls if cls_name == main_name else cls_def)
        for cls_name, cls_def in (schema.get("classes") or {}).items()
    }
    out["slots"] = new_slots

    old_enums = schema.get("enums") or {}
    if old_enums:
        refs = referenced_enums(new_slots)
        new_enums = {name: defn for name, defn in old_enums.items() if name in refs}
        if new_enums:
            out["enums"] = new_enums

    return out


def _warn_unknown(label: str, names: Sequence[str] | None, known: set[str]) -> None:
    if names is None:
        return
    unknown = [n for n in names if n not in known]
    if not unknown:
        return
    shown = ", ".join(unknown[:5]) + ("..." if len(unknown) > 5 else "")
    print(f"Warning: {label} list contains {len(unknown)} field(s) not in schema: {shown}",
          file=sys.stderr)
