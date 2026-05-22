#!/usr/bin/env python3
"""Build a DataHarmonizer LinkML YAML schema from XSD, XML, and/or LinkML inputs.

Integrates XSD conversion, XML conversion, schema merging, and slot filtering
into a single CLI command. Automatically detects input file types by extension.

Pipeline:
    Input files (any mix of .xsd, .xml, .yaml/.yml)
           │
           ▼
    Auto-detect and convert to LinkML dicts
           │
           ▼
    Merge all schemas (input order = priority)
           │
           ▼
    Apply include/exclude filtering (optional)
           │
           ▼
    Write merged LinkML YAML

Usage:
    python scripts/build_linkml.py input1.yaml input2.xsd input3.xml -o out.yaml
    python scripts/build_linkml.py schemas/*.yaml assets/ena_schema/*.xsd -o merged.yaml
    python scripts/build_linkml.py a.yaml b.xsd --include include.txt --exclude exclude.txt -o out.yaml
"""

import argparse
import os
import sys
from pathlib import Path

# Allow importing sibling scripts as modules when run directly or as a library.
sys.path.insert(0, str(Path(__file__).parent))

from ena_to_linkml import parse_checklist_xml, convert_to_linkml
from xsd_to_linkml import convert_xsd_to_linkml
from merge_linkml import merge_schemas, build_merged_schema
from filter_linkml import filter_schema, load_field_list, load_schema as load_linkml, write_yaml


# ---------------------------------------------------------------------------
# File type detection
# ---------------------------------------------------------------------------

def detect_file_type(filepath):
    """Detect input file type by extension. Returns 'linkml', 'xsd', 'xml', or None."""
    ext = os.path.splitext(filepath)[1].lower()
    if ext in (".yaml", ".yml"):
        return "linkml"
    if ext == ".xsd":
        return "xsd"
    if ext == ".xml":
        return "xml"
    return None


# ---------------------------------------------------------------------------
# Public library API
# ---------------------------------------------------------------------------

def convert_file(filepath, base_uri):
    """Convert a single input file to a LinkML schema dict, or None if unsupported/failed."""
    file_type = detect_file_type(filepath)
    if file_type == "linkml":
        return load_linkml(filepath)
    if file_type == "xsd":
        return convert_xsd_to_linkml(filepath, base_uri)
    if file_type == "xml":
        checklist = parse_checklist_xml(filepath)
        return convert_to_linkml(checklist, base_uri) if checklist is not None else None
    return None


def build_schema(
    input_files,
    *,
    base_uri="https://github.com/timrozday/ena-submission-dataharmonizer",
    name=None,
    title=None,
    description=None,
    include=None,
    exclude=None,
):
    """Convert, merge, and optionally filter a list of input files into one LinkML schema.

    Parameters
    ----------
    input_files : list[str]
        Paths to .xsd, .xml, or .yaml/.yml files, in priority order (first = highest).
    base_uri : str
        Base URI for schema id.
    name, title, description : str or None
        Override output schema metadata (defaults to highest-priority input values).
    include, exclude : list[str] or None
        Slot names to include/exclude after merging.

    Returns
    -------
    dict — complete merged and filtered LinkML schema dict.

    Raises
    ------
    ValueError if no valid schemas are found in input_files.
    """
    schemas = [s for f in input_files if (s := convert_file(f, base_uri)) is not None]

    if not schemas:
        raise ValueError("No valid schemas found in input files")

    source_names = [os.path.splitext(os.path.basename(p))[0] for p in input_files[:len(schemas)]]
    merge_result = merge_schemas(schemas, source_names=source_names)
    schema = build_merged_schema(
        merge_result, schemas,
        name=name, title=title, description=description, base_uri=base_uri,
    )

    if include is not None or exclude is not None:
        schema = filter_schema(schema, include=include, exclude=exclude)

    return schema


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Build a DataHarmonizer LinkML YAML schema from XSD, XML, and/or LinkML inputs. "
            "Automatically detects file types by extension (.xsd, .xml, .yaml/.yml). "
            "Input order determines merge priority (first = highest)."
        ),
    )
    parser.add_argument(
        "input_files", nargs="+",
        help=(
            "Input files to process. Can be any mix of: "
            ".xsd (ENA/SRA schema), .xml (ENA checklist), .yaml/.yml (LinkML). "
            "Order determines merge priority (first file = highest priority)."
        ),
    )
    parser.add_argument("-o", "--output", required=True, help="Path for the output YAML file.")
    parser.add_argument("--include", default=None, metavar="FILE",
                        help="Text file with field names to include (one per line).")
    parser.add_argument("--exclude", default=None, metavar="FILE",
                        help="Text file with field names to exclude (one per line).")
    parser.add_argument("--name", default=None,
                        help="Schema name for the output (default: from highest-priority input).")
    parser.add_argument("--title", default=None,
                        help="Schema title for the output (default: from highest-priority input).")
    parser.add_argument("--description", default=None,
                        help="Schema description for the output (default: from highest-priority input).")
    parser.add_argument(
        "--base-uri", default="https://github.com/timrozday/ena-submission-dataharmonizer",
        help="Base URI for the schema id (default: %(default)s).",
    )
    args = parser.parse_args()

    all_files = list(args.input_files)
    if args.include:
        all_files.append(args.include)
    if args.exclude:
        all_files.append(args.exclude)
    for path in all_files:
        if not os.path.isfile(path):
            print(f"Error: file not found: {path}", file=sys.stderr)
            sys.exit(1)

    print(f"Processing {len(args.input_files)} input file(s)...")
    for filepath in args.input_files:
        file_type = detect_file_type(filepath)
        print(f"  [{file_type or 'unknown'}] {filepath}")

    include = load_field_list(args.include) if args.include else None
    exclude = load_field_list(args.exclude) if args.exclude else None
    if include is not None:
        print(f"  Include list: {len(include)} field(s) from {args.include}")
    if exclude is not None:
        print(f"  Exclude list: {len(exclude)} field(s) from {args.exclude}")

    try:
        schema = build_schema(
            args.input_files,
            base_uri=args.base_uri,
            name=args.name,
            title=args.title,
            description=args.description,
            include=include,
            exclude=exclude,
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    write_yaml(schema, args.output)
    print(f"\nWritten: {args.output}")

    n_slots = len(schema.get("slots", {}))
    n_required = sum(1 for s in schema.get("slots", {}).values() if s.get("required"))
    n_enums = len(schema.get("enums", {}))
    print(f"  {n_slots} slots ({n_required} required), {n_enums} enums")
    print("Done.")


if __name__ == "__main__":
    main()
