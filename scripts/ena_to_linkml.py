#!/usr/bin/env python3
"""Convert ENA checklist XML files to DataHarmonizer-compatible LinkML YAML schemas.

Parses ENA sample checklist XML files (e.g. ERC000015.xml) and produces
LinkML YAML schema files suitable for use with DataHarmonizer.

Usage:
    python scripts/ena_to_linkml.py
    python scripts/ena_to_linkml.py assets/ena_schema/ERC000015.xml
    python scripts/ena_to_linkml.py -i assets/ena_schema/ -o schemas/
"""

import argparse
import os
import xml.etree.ElementTree as ET

import yaml


# ---------------------------------------------------------------------------
# YAML helpers – ensure LinkML-compatible output
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
# XML parsing
# ---------------------------------------------------------------------------

def parse_checklist_xml(filepath):
    """Parse an ENA checklist XML file and return a structured dict.

    Returns
    -------
    dict with keys: accession, checklist_type, label, name, description,
                    authority, field_groups (list of group dicts).
    Each group dict has: name, restriction_type, fields (list of field dicts).
    Each field dict has: label, name, description, field_type, regex_value,
                         choices, units, mandatory, multiplicity.
    """
    tree = ET.parse(filepath)
    root = tree.getroot()

    checklist = root.find("CHECKLIST")
    if checklist is None:
        # Root might be CHECKLIST directly
        checklist = root

    accession = checklist.get("accession", "")
    checklist_type = checklist.get("checklistType", "")

    descriptor = checklist.find("DESCRIPTOR")

    result = {
        "accession": accession,
        "checklist_type": checklist_type,
        "label": _text(descriptor, "LABEL"),
        "name": _text(descriptor, "NAME"),
        "description": _text(descriptor, "DESCRIPTION"),
        "authority": _text(descriptor, "AUTHORITY"),
        "field_groups": [],
    }

    for fg in descriptor.findall("FIELD_GROUP"):
        group = {
            "name": _text(fg, "NAME"),
            "restriction_type": fg.get("restrictionType", ""),
            "fields": [],
        }
        for field_el in fg.findall("FIELD"):
            group["fields"].append(_parse_field(field_el))
        result["field_groups"].append(group)

    return result


def _text(parent, tag):
    """Return text content of a child element, or empty string."""
    el = parent.find(tag)
    return (el.text or "").strip() if el is not None else ""


def _parse_field(field_el):
    """Parse a single FIELD element into a dict."""
    field = {
        "label": _text(field_el, "LABEL"),
        "name": _text(field_el, "NAME"),
        "description": _text(field_el, "DESCRIPTION"),
        "field_type": None,
        "regex_value": None,
        "choices": [],
        "units": [],
        "mandatory": _text(field_el, "MANDATORY"),
        "multiplicity": _text(field_el, "MULTIPLICITY"),
    }

    ft = field_el.find("FIELD_TYPE")
    if ft is not None:
        text_field = ft.find("TEXT_FIELD")
        choice_field = ft.find("TEXT_CHOICE_FIELD")

        if choice_field is not None:
            field["field_type"] = "TEXT_CHOICE_FIELD"
            for tv in choice_field.findall("TEXT_VALUE"):
                val = _text(tv, "VALUE")
                if val:
                    field["choices"].append(val)
        elif text_field is not None:
            field["field_type"] = "TEXT_FIELD"
            regex_el = text_field.find("REGEX_VALUE")
            if regex_el is not None and regex_el.text:
                field["regex_value"] = regex_el.text.strip()
        else:
            field["field_type"] = "TEXT_FIELD"

    units_el = field_el.find("UNITS")
    if units_el is not None:
        for u in units_el.findall("UNIT"):
            if u.text:
                field["units"].append(u.text.strip())

    return field


# ---------------------------------------------------------------------------
# Name helpers
# ---------------------------------------------------------------------------

def _make_enum_name(field_name):
    """Convert snake_case field name to PascalCaseMenu.

    Example: 'trophic_level' -> 'TrophicLevelMenu'
    """
    parts = field_name.split("_")
    return "".join(p.capitalize() for p in parts) + "Menu"


def _make_schema_name(accession):
    """Return a safe schema name string from an accession."""
    return accession


# ---------------------------------------------------------------------------
# LinkML conversion
# ---------------------------------------------------------------------------

def convert_to_linkml(checklist, base_uri):
    """Convert a parsed checklist dict into a LinkML schema dict.

    Parameters
    ----------
    checklist : dict
        Output of ``parse_checklist_xml``.
    base_uri : str
        Base URI for the schema ``id``.

    Returns
    -------
    dict suitable for YAML serialisation as a LinkML schema.
    """
    accession = checklist["accession"]
    schema_id = base_uri.rstrip("/") + "/" + accession

    schema = {
        "id": schema_id,
        "name": _make_schema_name(accession),
        "title": checklist["label"],
        "description": checklist["description"],
        "version": "1.0.0",
        "imports": ["linkml:types"],
        "prefixes": {
            "linkml": "https://w3id.org/linkml/",
            "ENA": "https://www.ebi.ac.uk/ena/browser/view/",
        },
        "default_range": "string",
    }

    # -- build slots, enums, slot list, and slot_usage ---
    slots = {}
    enums = {}
    slot_names = []
    slot_usage = {}
    rank = 1

    for group in checklist["field_groups"]:
        for field in group["fields"]:
            slot = _build_slot(field)
            slots[field["name"]] = slot
            slot_names.append(field["name"])

            usage = {"rank": rank, "slot_group": group["name"]}
            slot_usage[field["name"]] = usage
            rank += 1

            if field["field_type"] == "TEXT_CHOICE_FIELD" and field["choices"]:
                enum = _build_enum(field)
                enums[enum["name"]] = enum

    # -- classes ---
    main_class = {
        "name": accession,
        "title": checklist["label"],
        "description": checklist["description"],
        "is_a": "dh_interface",
        "slots": list(slot_names),
        "slot_usage": slot_usage,
    }

    schema["classes"] = {
        "dh_interface": {
            "name": "dh_interface",
            "description": "A DataHarmonizer interface",
            "from_schema": schema_id,
        },
        accession: main_class,
    }
    schema["slots"] = slots
    if enums:
        schema["enums"] = enums

    return schema


def _build_slot(field):
    """Build a LinkML slot dict from a parsed field dict."""
    slot = {
        "name": field["name"],
        "title": field["label"],
        "description": field["description"],
    }

    # Determine range
    if field["field_type"] == "TEXT_CHOICE_FIELD" and field["choices"]:
        slot["range"] = _make_enum_name(field["name"])
    else:
        slot["range"] = "string"

    # Required
    if field["mandatory"] == "mandatory":
        slot["required"] = True

    # Regex pattern
    if field["regex_value"]:
        slot["pattern"] = field["regex_value"]

    # Units as comments
    if field["units"]:
        slot["comments"] = ["Allowed units: " + ", ".join(field["units"])]

    return slot


def _build_enum(field):
    """Build a LinkML enum dict from a TEXT_CHOICE_FIELD."""
    name = _make_enum_name(field["name"])
    pvs = {}
    for val in field["choices"]:
        pvs[val] = {"text": val}
    return {"name": name, "permissible_values": pvs}


# ---------------------------------------------------------------------------
# YAML output
# ---------------------------------------------------------------------------

def write_yaml(schema, output_path):
    """Write a LinkML schema dict to a YAML file."""
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        yaml.dump(
            schema,
            f,
            Dumper=_LinkMLDumper,
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True,
            width=120,
        )
    print(f"  Written: {output_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Convert ENA checklist XML files to DataHarmonizer LinkML YAML.",
    )
    parser.add_argument(
        "input_files",
        nargs="*",
        help="XML file(s) to convert. If omitted, all *.xml in --input-dir are processed.",
    )
    parser.add_argument(
        "-i", "--input-dir",
        default="assets/ena_schema",
        help="Directory containing ENA XML files (default: assets/ena_schema).",
    )
    parser.add_argument(
        "-o", "--output-dir",
        default="schemas",
        help="Directory for output LinkML YAML files (default: schemas).",
    )
    parser.add_argument(
        "--base-uri",
        default="https://github.com/timrozday/ena-submission-dataharmonizer",
        help="Base URI for schema id.",
    )
    args = parser.parse_args()

    # Resolve input files
    if args.input_files:
        xml_files = args.input_files
    else:
        xml_files = sorted(
            os.path.join(args.input_dir, f)
            for f in os.listdir(args.input_dir)
            if f.lower().endswith(".xml")
        )

    if not xml_files:
        print(f"No XML files found in {args.input_dir}")
        return

    print(f"Processing {len(xml_files)} file(s)...")
    for xml_path in xml_files:
        print(f"\n  Parsing: {xml_path}")
        checklist = parse_checklist_xml(xml_path)

        schema = convert_to_linkml(checklist, args.base_uri)

        out_name = checklist["accession"] + ".yaml"
        out_path = os.path.join(args.output_dir, out_name)
        write_yaml(schema, out_path)

        # Summary
        n_slots = len(schema["slots"])
        n_required = sum(1 for s in schema["slots"].values() if s.get("required"))
        n_enums = len(schema.get("enums", {}))
        print(f"  Fields: {n_slots} ({n_required} required), Enums: {n_enums}")

    print("\nDone.")


if __name__ == "__main__":
    main()
