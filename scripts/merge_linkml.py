#!/usr/bin/env python3
"""Merge multiple DataHarmonizer LinkML YAML schema files into one.

Reads two or more LinkML YAML files and produces a single merged schema.
Input files are listed in priority order: the first file has the highest
priority.  When identically named slots, enums, or classes appear in
more than one input, the definition from the highest-priority file wins.

Slots from all inputs are included in the merged output.  Ordering is
determined by walking the inputs from highest to lowest priority and
appending each slot the first time it is seen, so the highest-priority
file's order is preserved and additional slots from lower-priority files
follow.  Ranks are renumbered sequentially in the merged output.

Usage:
    python scripts/merge_linkml.py schemas/ERC000015.yaml schemas/ERC000025.yaml -o schemas/merged.yaml
    python scripts/merge_linkml.py a.yaml b.yaml c.yaml --name MySchema --title "My merged schema"
"""

import argparse
import os
import sys

import yaml


# ---------------------------------------------------------------------------
# YAML helpers – same dumper as ena_to_linkml.py for consistent output
# ---------------------------------------------------------------------------

class _LinkMLDumper(yaml.SafeDumper):
    """Custom YAML dumper that emits lowercase booleans and preserves order."""
    pass


def _bool_representer(dumper, data):
    return dumper.represent_scalar("tag:yaml.org,2002:bool", "true" if data else "false")


def _str_representer(dumper, data):
    """Use literal block style for multi-line strings, otherwise default."""
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_LinkMLDumper.add_representer(bool, _bool_representer)
_LinkMLDumper.add_representer(str, _str_representer)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_schema(filepath):
    """Load a LinkML YAML schema file and return the parsed dict."""
    with open(filepath, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------

def _get_main_class(schema):
    """Return (name, class_dict) for the main class (the one that is_a dh_interface)."""
    for name, cls in schema.get("classes", {}).items():
        if cls.get("is_a") == "dh_interface":
            return name, cls
    return None, None


def _ordered_slot_names(schema):
    """Return the slot names listed in the main class, preserving order."""
    _, main_cls = _get_main_class(schema)
    if main_cls is None:
        return []
    return list(main_cls.get("slots", []))


# ---------------------------------------------------------------------------
# Merging
# ---------------------------------------------------------------------------

def merge_schemas(schemas, source_names=None):
    """Merge a list of parsed schema dicts (highest priority first).

    Parameters
    ----------
    schemas : list[dict]
        Parsed schema dicts, highest priority first.
    source_names : list[str] or None
        Optional source names (one per schema) used to prefix slot_group
        values as ``source_name:slot_group`` and to set a ``source``
        attribute on each slot indicating which input file it came from.
        When *None*, slot_group values are left unchanged and no source
        attribute is added.

    Returns a merged schema dict ready for YAML serialisation.
    """
    if not schemas:
        return {}

    # -- collect slots, enums, and slot_usage from all inputs ---------------
    # Use dicts keyed by name; first occurrence (highest priority) wins.
    merged_slots = {}
    merged_enums = {}
    merged_slot_usage = {}
    seen_slot_order = []  # ordered unique slot names

    for idx, schema in enumerate(schemas):
        source_prefix = source_names[idx] if source_names and idx < len(source_names) else None
        _, main_cls = _get_main_class(schema)
        schema_slots = schema.get("slots", {})
        slot_usage = main_cls.get("slot_usage", {}) if main_cls else {}

        for slot_name in _ordered_slot_names(schema):
            if slot_name not in merged_slots:
                seen_slot_order.append(slot_name)

            if slot_name not in merged_slots and slot_name in schema_slots:
                slot_def = dict(schema_slots[slot_name])
                if source_prefix:
                    slot_def["source"] = source_prefix
                merged_slots[slot_name] = slot_def

            if slot_name not in merged_slot_usage and slot_name in slot_usage:
                merged_slot_usage[slot_name] = dict(slot_usage[slot_name])

        for enum_name, enum_def in schema.get("enums", {}).items():
            if enum_name not in merged_enums:
                merged_enums[enum_name] = enum_def

    # Also pick up any slot definitions that exist in the slots section but
    # were not referenced in any main class's slot list (unlikely but safe).
    for idx, schema in enumerate(schemas):
        source_prefix = source_names[idx] if source_names and idx < len(source_names) else None
        for slot_name, slot_def in schema.get("slots", {}).items():
            if slot_name not in merged_slots:
                slot_def = dict(slot_def)
                if source_prefix:
                    slot_def["source"] = source_prefix
                merged_slots[slot_name] = slot_def
                seen_slot_order.append(slot_name)

    # -- renumber ranks sequentially ----------------------------------------
    renumbered_usage = {}
    for rank, slot_name in enumerate(seen_slot_order, start=1):
        usage = dict(merged_slot_usage.get(slot_name, {}))
        usage["rank"] = rank
        renumbered_usage[slot_name] = usage

    return {
        "slot_order": seen_slot_order,
        "slots": merged_slots,
        "enums": merged_enums,
        "slot_usage": renumbered_usage,
    }


def build_merged_schema(merge_result, schemas, name, title, description, base_uri):
    """Assemble a complete LinkML schema dict from merge results and metadata.

    Parameters
    ----------
    merge_result : dict
        Output of ``merge_schemas``.
    schemas : list[dict]
        Original parsed schemas (highest priority first), used to derive
        defaults for metadata fields the caller did not supply.
    name : str or None
        Schema name; defaults to the highest-priority schema's name.
    title : str or None
        Schema title; defaults to the highest-priority schema's title.
    description : str or None
        Schema description; defaults to the highest-priority schema's
        description.
    base_uri : str or None
        Base URI for the schema id; defaults to the highest-priority
        schema's base URI (id minus the trailing /name segment).
    """
    first = schemas[0]

    if name is None:
        name = first.get("name", "merged")
    if title is None:
        title = first.get("title", name)
    if description is None:
        description = first.get("description", "")

    if base_uri is None:
        # Derive from the first schema's id by stripping the last path segment.
        first_id = first.get("id", "")
        base_uri = first_id.rsplit("/", 1)[0] if "/" in first_id else first_id

    schema_id = base_uri.rstrip("/") + "/" + name

    # -- prefixes: merge all (higher priority wins on conflicts) ------------
    merged_prefixes = {}
    for s in reversed(schemas):
        merged_prefixes.update(s.get("prefixes", {}))

    schema = {
        "id": schema_id,
        "name": name,
        "title": title,
        "description": description,
        "version": first.get("version", "1.0.0"),
        "imports": first.get("imports", ["linkml:types"]),
        "prefixes": merged_prefixes,
        "default_range": first.get("default_range", "string"),
    }

    # -- classes ------------------------------------------------------------
    main_class = {
        "name": name,
        "title": title,
        "description": description,
        "is_a": "dh_interface",
        "slots": list(merge_result["slot_order"]),
        "slot_usage": merge_result["slot_usage"],
    }

    schema["classes"] = {
        "dh_interface": {
            "name": "dh_interface",
            "description": "A DataHarmonizer interface",
            "from_schema": schema_id,
        },
        name: main_class,
    }

    schema["slots"] = merge_result["slots"]

    if merge_result["enums"]:
        schema["enums"] = merge_result["enums"]

    return schema


# ---------------------------------------------------------------------------
# YAML output
# ---------------------------------------------------------------------------

def write_yaml(schema, output_path):
    """Write a LinkML schema dict to a YAML file."""
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        yaml.dump(schema, f, Dumper=_LinkMLDumper, default_flow_style=False,
                  sort_keys=False, allow_unicode=True, width=120)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Merge multiple DataHarmonizer LinkML YAML schemas into one.",
    )
    parser.add_argument(
        "input_files",
        nargs="+",
        help=(
            "LinkML YAML files to merge, listed from highest to lowest priority. "
            "When identically named entries exist in multiple files the definition "
            "from the highest-priority (earliest listed) file is used."
        ),
    )
    parser.add_argument(
        "-o", "--output",
        required=True,
        help="Path for the merged output YAML file.",
    )
    parser.add_argument(
        "--name",
        default=None,
        help="Schema name for the merged output (default: taken from the highest-priority input).",
    )
    parser.add_argument(
        "--title",
        default=None,
        help="Schema title for the merged output (default: taken from the highest-priority input).",
    )
    parser.add_argument(
        "--description",
        default=None,
        help="Schema description for the merged output (default: taken from the highest-priority input).",
    )
    parser.add_argument(
        "--base-uri",
        default=None,
        help="Base URI for the schema id (default: derived from the highest-priority input).",
    )

    args = parser.parse_args()

    # -- validate inputs ----------------------------------------------------
    for path in args.input_files:
        if not os.path.isfile(path):
            print(f"Error: file not found: {path}", file=sys.stderr)
            sys.exit(1)

    # -- load ---------------------------------------------------------------
    schemas = []
    for path in args.input_files:
        print(f"  Loading: {path}")
        schemas.append(load_schema(path))

    # -- merge --------------------------------------------------------------
    source_names = [os.path.splitext(os.path.basename(p))[0] for p in args.input_files]
    merge_result = merge_schemas(schemas, source_names=source_names)

    schema = build_merged_schema(
        merge_result,
        schemas,
        name=args.name,
        title=args.title,
        description=args.description,
        base_uri=args.base_uri,
    )

    # -- write --------------------------------------------------------------
    write_yaml(schema, args.output)
    print(f"  Written: {args.output}")

    # -- summary ------------------------------------------------------------
    n_slots = len(schema["slots"])
    n_required = sum(1 for s in schema["slots"].values() if s.get("required"))
    n_enums = len(schema.get("enums", {}))
    n_inputs = len(args.input_files)
    print(f"\n  Merged {n_inputs} schemas -> {n_slots} slots ({n_required} required), {n_enums} enums")
    print("Done.")


if __name__ == "__main__":
    main()
