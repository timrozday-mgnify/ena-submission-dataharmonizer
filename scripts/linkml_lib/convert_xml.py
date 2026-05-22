"""Convert ENA checklist XML files to LinkML schema dicts.

Public functions:
    from_path(path, base_uri) -> dict | None
    parse_checklist(path) -> dict | None              # raw structured dict
    to_linkml(checklist, base_uri) -> dict            # parsed dict → schema dict
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


def from_path(path: str | Path, base_uri: str) -> dict[str, Any] | None:
    """Load an ENA checklist XML file and convert it to a LinkML schema dict."""
    checklist = parse_checklist(path)
    return to_linkml(checklist, base_uri) if checklist is not None else None


def parse_checklist(path: str | Path) -> dict[str, Any] | None:
    """Parse an ENA checklist XML file into a structured dict.

    Returns None if the file has no DESCRIPTOR element.
    """
    tree = ET.parse(str(path))
    root = tree.getroot()
    checklist = root.find("CHECKLIST") or root
    descriptor = checklist.find("DESCRIPTOR")
    if descriptor is None:
        return None

    return {
        "accession": checklist.get("accession", ""),
        "checklist_type": checklist.get("checklistType", ""),
        "label": _text(descriptor, "LABEL"),
        "name": _text(descriptor, "NAME"),
        "description": _text(descriptor, "DESCRIPTION"),
        "authority": _text(descriptor, "AUTHORITY"),
        "field_groups": [
            {
                "name": _text(fg, "NAME"),
                "restriction_type": fg.get("restrictionType", ""),
                "fields": [_parse_field(f) for f in fg.findall("FIELD")],
            }
            for fg in descriptor.findall("FIELD_GROUP")
        ],
    }


def to_linkml(checklist: dict[str, Any], base_uri: str) -> dict[str, Any]:
    """Convert a parsed checklist dict to a LinkML schema dict."""
    accession = checklist["accession"]
    schema_id = base_uri.rstrip("/") + "/" + accession

    slots: dict[str, dict[str, Any]] = {}
    enums: dict[str, dict[str, Any]] = {}
    slot_names: list[str] = []
    slot_usage: dict[str, dict[str, Any]] = {}

    rank = 1
    for group in checklist["field_groups"]:
        for field in group["fields"]:
            slot = _build_slot(field)
            slots[field["name"]] = slot
            slot_names.append(field["name"])
            slot_usage[field["name"]] = {"rank": rank, "slot_group": group["name"]}
            rank += 1
            if field["field_type"] == "TEXT_CHOICE_FIELD" and field["choices"]:
                enum = _build_enum(field)
                enums[enum["name"]] = enum

    main_class = {
        "name": accession,
        "title": checklist["label"],
        "description": checklist["description"],
        "is_a": "dh_interface",
        "slots": list(slot_names),
        "slot_usage": slot_usage,
    }

    schema: dict[str, Any] = {
        "id": schema_id,
        "name": accession,
        "title": checklist["label"],
        "description": checklist["description"],
        "version": "1.0.0",
        "imports": ["linkml:types"],
        "prefixes": {
            "linkml": "https://w3id.org/linkml/",
            "ENA": "https://www.ebi.ac.uk/ena/browser/view/",
        },
        "default_range": "string",
        "classes": {
            "dh_interface": {
                "name": "dh_interface",
                "description": "A DataHarmonizer interface",
                "from_schema": schema_id,
            },
            accession: main_class,
        },
        "slots": slots,
    }
    if enums:
        schema["enums"] = enums
    return schema


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _text(parent: ET.Element, tag: str) -> str:
    el = parent.find(tag)
    return (el.text or "").strip() if el is not None and el.text else ""


def _parse_field(field_el: ET.Element) -> dict[str, Any]:
    field: dict[str, Any] = {
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
        choice_field = ft.find("TEXT_CHOICE_FIELD")
        text_field = ft.find("TEXT_FIELD")
        if choice_field is not None:
            field["field_type"] = "TEXT_CHOICE_FIELD"
            field["choices"] = [v for tv in choice_field.findall("TEXT_VALUE")
                                if (v := _text(tv, "VALUE"))]
        elif text_field is not None:
            field["field_type"] = "TEXT_FIELD"
            regex_el = text_field.find("REGEX_VALUE")
            if regex_el is not None and regex_el.text:
                field["regex_value"] = regex_el.text.strip()
        else:
            field["field_type"] = "TEXT_FIELD"

    units_el = field_el.find("UNITS")
    if units_el is not None:
        field["units"] = [u.text.strip() for u in units_el.findall("UNIT") if u.text]

    return field


def _make_enum_name(field_name: str) -> str:
    """Convert a snake/kebab-case field name to PascalCaseMenu."""
    parts = field_name.replace("-", "_").split("_")
    return "".join(p.capitalize() for p in parts) + "Menu"


def _build_slot(field: dict[str, Any]) -> dict[str, Any]:
    slot: dict[str, Any] = {
        "name": field["name"],
        "title": field["label"],
        "description": field["description"],
    }
    if field["field_type"] == "TEXT_CHOICE_FIELD" and field["choices"]:
        slot["range"] = _make_enum_name(field["name"])
    else:
        slot["range"] = "string"
    if field["mandatory"] == "mandatory":
        slot["required"] = True
    if field["regex_value"]:
        slot["pattern"] = field["regex_value"]
    if field["units"]:
        slot["comments"] = ["Allowed units: " + ", ".join(field["units"])]
    return slot


def _build_enum(field: dict[str, Any]) -> dict[str, Any]:
    name = _make_enum_name(field["name"])
    return {
        "name": name,
        "permissible_values": {val: {"text": val} for val in field["choices"]},
    }
