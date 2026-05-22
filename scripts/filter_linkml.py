#!/usr/bin/env python3
"""Filter slots in a DataHarmonizer LinkML YAML schema by include/exclude lists.

Reads a LinkML YAML schema file and filters its slots (fields) based on
newline-separated include and/or exclude text files.

Filter logic:
- Include only: keep only the listed fields.
- Exclude only: keep all fields except the listed ones.
- Both: start with the include list, then remove any in the exclude list.

When a slot is removed its entry is deleted from the main class's slot list,
slot_usage, the top-level slots section, and any enums that are no longer
referenced by a remaining slot's range.  Ranks are renumbered sequentially.

Usage:
    python scripts/filter_linkml.py schemas/ERC000015.yaml -o filtered.yaml --exclude exclude.txt
    python scripts/filter_linkml.py schemas/ERC000015.yaml -o filtered.yaml --include include.txt
    python scripts/filter_linkml.py schemas/ERC000015.yaml -o filtered.yaml --include inc.txt --exclude exc.txt
"""

import argparse
import os
import sys

import yaml


# ---------------------------------------------------------------------------
# YAML helpers – same dumper as ena_to_linkml.py / merge_linkml.py
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


def load_field_list(filepath):
    """Read a newline-separated text file of field names.

    Strips whitespace, skips blank lines and lines starting with ``#``.
    """
    with open(filepath, "r", encoding="utf-8") as f:
        names = []
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                names.append(line)
        return names


# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------

def _get_main_class(schema):
    """Return (name, class_dict) for the main class (the one that is_a dh_interface)."""
    for name, cls in schema.get("classes", {}).items():
        if isinstance(cls, dict) and cls.get("is_a") == "dh_interface":
            return name, cls
    return None, None


def _referenced_enums(slots_dict):
    """Return the set of enum names referenced by remaining slots' ``range``."""
    refs = set()
    for slot_def in slots_dict.values():
        r = slot_def.get("range", "")
        if r:
            refs.add(r)
    return refs


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

def filter_schema(schema, include=None, exclude=None):
    """Filter slots in *schema* and return a new schema dict.

    Parameters
    ----------
    schema : dict
        Parsed LinkML schema.
    include : list[str] or None
        If given, only these slot names are kept.
    exclude : list[str] or None
        If given, these slot names are removed (applied after include).

    Returns
    -------
    dict — a filtered copy of the schema.
    """
    main_name, main_cls = _get_main_class(schema)
    if main_cls is None:
        print("Warning: no main class (is_a: dh_interface) found in schema", file=sys.stderr)
        return schema

    all_slot_names = list(main_cls.get("slots", []))
    all_slot_set = set(all_slot_names)

    # -- warn about unknown field names ------------------------------------
    if include is not None:
        unknown = [n for n in include if n not in all_slot_set]
        if unknown:
            print(f"Warning: include list contains {len(unknown)} field(s) not in schema: "
                  f"{', '.join(unknown)}", file=sys.stderr)

    if exclude is not None:
        unknown = [n for n in exclude if n not in all_slot_set]
        if unknown:
            print(f"Warning: exclude list contains {len(unknown)} field(s) not in schema: "
                  f"{', '.join(unknown)}", file=sys.stderr)

    # -- determine which slots to keep -------------------------------------
    if include is not None:
        # Preserve the order from the original schema, restricted to include set.
        include_set = set(include)
        kept = [s for s in all_slot_names if s in include_set]
    else:
        kept = list(all_slot_names)

    if exclude is not None:
        exclude_set = set(exclude)
        kept = [s for s in kept if s not in exclude_set]

    # -- build filtered schema ---------------------------------------------
    filtered = {}
    for key, value in schema.items():
        if key in ("classes", "slots", "enums"):
            continue
        filtered[key] = value

    # -- classes ------------------------------------------------------------
    old_slot_usage = main_cls.get("slot_usage", {})
    new_slot_usage = {}
    for rank, slot_name in enumerate(kept, start=1):
        usage = dict(old_slot_usage.get(slot_name, {}))
        usage["rank"] = rank
        new_slot_usage[slot_name] = usage

    new_main_cls = {}
    for key, value in main_cls.items():
        if key == "slots":
            new_main_cls["slots"] = list(kept)
        elif key == "slot_usage":
            new_main_cls["slot_usage"] = new_slot_usage
        else:
            new_main_cls[key] = value

    filtered["classes"] = {}
    for cls_name, cls_def in schema.get("classes", {}).items():
        if cls_name == main_name:
            filtered["classes"][cls_name] = new_main_cls
        else:
            filtered["classes"][cls_name] = cls_def

    # -- top-level slots ----------------------------------------------------
    old_slots = schema.get("slots", {})
    new_slots = {name: old_slots[name] for name in kept if name in old_slots}
    filtered["slots"] = new_slots

    # -- enums: keep only those still referenced ----------------------------
    old_enums = schema.get("enums", {})
    if old_enums:
        refs = _referenced_enums(new_slots)
        new_enums = {name: defn for name, defn in old_enums.items() if name in refs}
        if new_enums:
            filtered["enums"] = new_enums

    return filtered


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
        description="Filter slots in a DataHarmonizer LinkML YAML schema by include/exclude lists.",
    )
    parser.add_argument(
        "input_file",
        help="LinkML YAML schema file to filter.",
    )
    parser.add_argument(
        "-o", "--output",
        required=True,
        help="Path for the filtered output YAML file.",
    )
    parser.add_argument(
        "--include",
        default=None,
        metavar="FILE",
        help="Text file with field names to include (one per line).",
    )
    parser.add_argument(
        "--exclude",
        default=None,
        metavar="FILE",
        help="Text file with field names to exclude (one per line).",
    )

    args = parser.parse_args()

    if args.include is None and args.exclude is None:
        parser.error("At least one of --include or --exclude is required.")

    if not os.path.isfile(args.input_file):
        print(f"Error: file not found: {args.input_file}", file=sys.stderr)
        sys.exit(1)

    # -- load ---------------------------------------------------------------
    print(f"  Loading: {args.input_file}")
    schema = load_schema(args.input_file)

    include = None
    exclude = None
    if args.include:
        include = load_field_list(args.include)
        print(f"  Include list: {len(include)} field(s) from {args.include}")
    if args.exclude:
        exclude = load_field_list(args.exclude)
        print(f"  Exclude list: {len(exclude)} field(s) from {args.exclude}")

    # -- filter -------------------------------------------------------------
    filtered = filter_schema(schema, include=include, exclude=exclude)

    # -- write --------------------------------------------------------------
    write_yaml(filtered, args.output)
    print(f"  Written: {args.output}")

    # -- summary ------------------------------------------------------------
    n_slots = len(filtered.get("slots", {}))
    n_required = sum(1 for s in filtered.get("slots", {}).values() if s.get("required"))
    n_enums = len(filtered.get("enums", {}))

    _, main_cls = _get_main_class(schema)
    n_original = len(main_cls.get("slots", [])) if main_cls else 0
    n_removed = n_original - n_slots

    print(f"\n  {n_original} -> {n_slots} slots ({n_removed} removed, {n_required} required), {n_enums} enums")
    print("Done.")


if __name__ == "__main__":
    main()
