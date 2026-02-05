#!/usr/bin/env python3
"""Build a DataHarmonizer LinkML YAML schema from XSD and/or LinkML inputs.

Chains XSD conversion, schema merging, and slot filtering into a single
CLI command.  Imports functions from the three sibling scripts rather than
duplicating code.

Pipeline:
    XSD files  --> convert_to_linkml --+
                                       +--> merge_schemas --> filter_schema --> write_yaml
    LinkML files ---------------------+
                  (higher priority)

Usage:
    python scripts/build_linkml.py --xsd assets/ena_schema/ERC000015.xml -o out.yaml
    python scripts/build_linkml.py --linkml schemas/ERC000015.yaml schemas/ERC000025.yaml -o out.yaml
    python scripts/build_linkml.py --linkml schemas/ERC000015.yaml --xsd assets/ena_schema/ERC000025.xml \
        --include include.txt --exclude exclude.txt -o out.yaml
"""

import argparse
import os
import sys

# Allow importing sibling scripts from the same directory.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ena_to_linkml import parse_checklist_xml, convert_to_linkml
from merge_linkml import load_schema, merge_schemas, build_merged_schema, write_yaml
from filter_linkml import load_field_list, filter_schema


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Build a DataHarmonizer LinkML YAML schema from XSD and/or LinkML inputs. "
            "Converts XSD files, merges all inputs, and optionally filters slots."
        ),
    )
    parser.add_argument(
        "--xsd",
        nargs="+",
        metavar="FILE",
        help="ENA checklist XML file(s) to convert. Order = priority (first = highest).",
    )
    parser.add_argument(
        "--linkml",
        nargs="+",
        metavar="FILE",
        help="Existing LinkML YAML file(s). Order = priority (first = highest). "
             "LinkML inputs have higher priority than XSD inputs.",
    )
    parser.add_argument(
        "-o", "--output",
        required=True,
        help="Path for the output YAML file.",
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
    parser.add_argument(
        "--name",
        default=None,
        help="Schema name for the output (default: derived from highest-priority input).",
    )
    parser.add_argument(
        "--title",
        default=None,
        help="Schema title for the output (default: derived from highest-priority input).",
    )
    parser.add_argument(
        "--description",
        default=None,
        help="Schema description for the output (default: derived from highest-priority input).",
    )
    parser.add_argument(
        "--base-uri",
        default="https://github.com/timrozday/ena-submission-dataharmonizer",
        help="Base URI for the schema id (default: %(default)s).",
    )

    args = parser.parse_args()

    # -- validate: at least one input source --------------------------------
    if not args.xsd and not args.linkml:
        parser.error("At least one of --xsd or --linkml is required.")

    # -- validate: all input files exist ------------------------------------
    all_input_files = (args.xsd or []) + (args.linkml or [])
    if args.include:
        all_input_files.append(args.include)
    if args.exclude:
        all_input_files.append(args.exclude)

    for path in all_input_files:
        if not os.path.isfile(path):
            print(f"Error: file not found: {path}", file=sys.stderr)
            sys.exit(1)

    # -- step 1: convert XSD files -----------------------------------------
    xsd_schemas = []
    if args.xsd:
        print(f"Converting {len(args.xsd)} XSD file(s)...")
        for xsd_path in args.xsd:
            print(f"  Parsing: {xsd_path}")
            checklist = parse_checklist_xml(xsd_path)
            schema = convert_to_linkml(checklist, args.base_uri)
            xsd_schemas.append(schema)

    # -- step 2: load LinkML files -----------------------------------------
    linkml_schemas = []
    if args.linkml:
        print(f"Loading {len(args.linkml)} LinkML file(s)...")
        for linkml_path in args.linkml:
            print(f"  Loading: {linkml_path}")
            linkml_schemas.append(load_schema(linkml_path))

    # -- step 3: merge (LinkML first = higher priority) --------------------
    all_schemas = linkml_schemas + xsd_schemas
    print(f"Merging {len(all_schemas)} schema(s)...")

    merge_result = merge_schemas(all_schemas)
    schema = build_merged_schema(
        merge_result,
        all_schemas,
        name=args.name,
        title=args.title,
        description=args.description,
        base_uri=args.base_uri,
    )

    # -- step 4: filter (optional) -----------------------------------------
    if args.include or args.exclude:
        include = None
        exclude = None
        if args.include:
            include = load_field_list(args.include)
            print(f"  Include list: {len(include)} field(s) from {args.include}")
        if args.exclude:
            exclude = load_field_list(args.exclude)
            print(f"  Exclude list: {len(exclude)} field(s) from {args.exclude}")
        schema = filter_schema(schema, include=include, exclude=exclude)

    # -- step 5: write -----------------------------------------------------
    write_yaml(schema, args.output)
    print(f"  Written: {args.output}")

    # -- summary -----------------------------------------------------------
    n_slots = len(schema.get("slots", {}))
    n_required = sum(1 for s in schema.get("slots", {}).values() if s.get("required"))
    n_enums = len(schema.get("enums", {}))
    print(f"\n  {n_slots} slots ({n_required} required), {n_enums} enums")
    print("Done.")


if __name__ == "__main__":
    main()
