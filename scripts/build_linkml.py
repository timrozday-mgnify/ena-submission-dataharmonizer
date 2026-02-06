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
# File type detection
# ---------------------------------------------------------------------------

def detect_file_type(filepath):
    """Detect input file type by extension and content.

    Returns one of: 'linkml', 'xsd', 'xml', or None if unknown.
    """
    ext = os.path.splitext(filepath)[1].lower()

    if ext in (".yaml", ".yml"):
        return "linkml"
    elif ext == ".xsd":
        return "xsd"
    elif ext == ".xml":
        return "xml"

    return None


# ---------------------------------------------------------------------------
# LinkML loading
# ---------------------------------------------------------------------------

def load_linkml(filepath):
    """Load a LinkML YAML schema file and return the parsed dict."""
    with open(filepath, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# ENA XML conversion (from ena_to_linkml.py)
# ---------------------------------------------------------------------------

def _xml_text(parent, tag):
    """Return text content of a child element, or empty string."""
    el = parent.find(tag)
    return (el.text or "").strip() if el is not None else ""


def _parse_xml_field(field_el):
    """Parse a single FIELD element from ENA checklist XML."""
    field = {
        "label": _xml_text(field_el, "LABEL"),
        "name": _xml_text(field_el, "NAME"),
        "description": _xml_text(field_el, "DESCRIPTION"),
        "field_type": None,
        "regex_value": None,
        "choices": [],
        "units": [],
        "mandatory": _xml_text(field_el, "MANDATORY"),
        "multiplicity": _xml_text(field_el, "MULTIPLICITY"),
    }

    ft = field_el.find("FIELD_TYPE")
    if ft is not None:
        text_field = ft.find("TEXT_FIELD")
        choice_field = ft.find("TEXT_CHOICE_FIELD")

        if choice_field is not None:
            field["field_type"] = "TEXT_CHOICE_FIELD"
            for tv in choice_field.findall("TEXT_VALUE"):
                val = _xml_text(tv, "VALUE")
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


def parse_ena_xml(filepath):
    """Parse an ENA checklist XML file and return a structured dict."""
    tree = ET.parse(filepath)
    root = tree.getroot()

    checklist = root.find("CHECKLIST")
    if checklist is None:
        checklist = root

    accession = checklist.get("accession", "")
    checklist_type = checklist.get("checklistType", "")

    descriptor = checklist.find("DESCRIPTOR")

    result = {
        "accession": accession,
        "checklist_type": checklist_type,
        "label": _xml_text(descriptor, "LABEL"),
        "name": _xml_text(descriptor, "NAME"),
        "description": _xml_text(descriptor, "DESCRIPTION"),
        "authority": _xml_text(descriptor, "AUTHORITY"),
        "field_groups": [],
    }

    for fg in descriptor.findall("FIELD_GROUP"):
        group = {
            "name": _xml_text(fg, "NAME"),
            "restriction_type": fg.get("restrictionType", ""),
            "fields": [],
        }
        for field_el in fg.findall("FIELD"):
            group["fields"].append(_parse_xml_field(field_el))
        result["field_groups"].append(group)

    return result


def _make_enum_name(field_name):
    """Convert snake_case field name to PascalCaseMenu."""
    parts = field_name.replace("-", "_").split("_")
    return "".join(p.capitalize() for p in parts) + "Menu"


def convert_ena_xml_to_linkml(filepath, base_uri):
    """Convert an ENA checklist XML file to a LinkML schema dict."""
    checklist = parse_ena_xml(filepath)

    accession = checklist["accession"]
    schema_id = base_uri.rstrip("/") + "/" + accession

    schema = {
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
    }

    slots = {}
    enums = {}
    slot_names = []
    slot_usage = {}
    rank = 1

    for group in checklist["field_groups"]:
        for field in group["fields"]:
            slot = {
                "name": field["name"],
                "title": field["label"],
                "description": field["description"],
            }

            if field["field_type"] == "TEXT_CHOICE_FIELD" and field["choices"]:
                enum_name = _make_enum_name(field["name"])
                slot["range"] = enum_name
                pvs = {}
                for val in field["choices"]:
                    pvs[val] = {"text": val}
                enums[enum_name] = {"name": enum_name, "permissible_values": pvs}
            else:
                slot["range"] = "string"

            if field["mandatory"] == "mandatory":
                slot["required"] = True

            if field["regex_value"]:
                slot["pattern"] = field["regex_value"]

            if field["units"]:
                slot["comments"] = ["Allowed units: " + ", ".join(field["units"])]

            slots[field["name"]] = slot
            slot_names.append(field["name"])
            slot_usage[field["name"]] = {"rank": rank, "slot_group": group["name"]}
            rank += 1

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


# ---------------------------------------------------------------------------
# XSD conversion (from xsd_to_linkml.py)
# ---------------------------------------------------------------------------

import re

XS_NS = "http://www.w3.org/2001/XMLSchema"
NS = {"xs": XS_NS}

SKIP_ELEMENT_PATTERNS = re.compile(
    r"^(.*_LINKS|.*_ATTRIBUTES|RELATED_.*)$", re.IGNORECASE
)
SKIP_COM_TYPES = {
    "com:LinkType",
    "com:AttributeType",
    "com:SpotDescriptorType",
    "com:ProcessingType",
    "com:ReferenceSequenceType",
    "com:XRefType",
    "com:PlatformType",
}

XSD_TO_LINKML_TYPE = {
    "xs:string": "string",
    "xs:int": "integer",
    "xs:integer": "integer",
    "xs:nonNegativeInteger": "integer",
    "xs:positiveInteger": "integer",
    "xs:float": "float",
    "xs:double": "float",
    "xs:decimal": "float",
    "xs:boolean": "boolean",
    "xs:date": "date",
    "xs:dateTime": "datetime",
    "xs:token": "string",
}


def _xsd_get_doc(elem):
    """Extract documentation text from xs:annotation/xs:documentation."""
    ann = elem.find("xs:annotation", NS)
    if ann is not None:
        doc = ann.find("xs:documentation", NS)
        if doc is not None and doc.text:
            return " ".join(doc.text.split())
    return ""


def _xsd_is_primitive_type(type_name):
    if type_name is None:
        return False
    return type_name.startswith("xs:") or type_name in XSD_TO_LINKML_TYPE


def _xsd_get_linkml_range(xsd_type):
    if xsd_type is None:
        return "string"
    return XSD_TO_LINKML_TYPE.get(xsd_type, "string")


def _xsd_extract_inline_enum(simple_type_elem):
    restriction = simple_type_elem.find("xs:restriction", NS)
    if restriction is None:
        return None
    enum_values = []
    for enum_elem in restriction.findall("xs:enumeration", NS):
        val = enum_elem.get("value")
        if val is not None:
            desc = _xsd_get_doc(enum_elem)
            enum_values.append({"value": val, "description": desc})
    return enum_values if enum_values else None


def _xsd_parse(filepath):
    tree = ET.parse(filepath)
    root = tree.getroot()
    complex_types = {}
    simple_types = {}
    for ct in root.findall("xs:complexType", NS):
        name = ct.get("name")
        if name:
            complex_types[name] = ct
    for st in root.findall("xs:simpleType", NS):
        name = st.get("name")
        if name:
            simple_types[name] = st
    return root, complex_types, simple_types


def _xsd_find_main_type(complex_types, filename):
    basename = os.path.basename(filename).lower()
    patterns = [
        ("study", "StudyType"),
        ("project", "ProjectType"),
        ("experiment", "ExperimentType"),
        ("run", "RunType"),
    ]
    for pattern, type_name in patterns:
        if pattern in basename and type_name in complex_types:
            return type_name, complex_types[type_name]
    for name, elem in complex_types.items():
        if name.endswith("Type") and not name.endswith("SetType"):
            return name, elem
    return None, None


class _XSDWalker:
    """Recursive walker that extracts fields from XSD complex types."""

    def __init__(self, complex_types, simple_types):
        self.complex_types = complex_types
        self.simple_types = simple_types
        self.slots = {}
        self.enums = {}
        self.seen_names = set()

    def _should_skip_element(self, name):
        return SKIP_ELEMENT_PATTERNS.match(name) is not None

    def _should_skip_type(self, type_ref):
        return type_ref in SKIP_COM_TYPES

    def _add_slot(self, name, description, range_type="string", required=False):
        if name in self.seen_names:
            return
        self.seen_names.add(name)
        slot = {
            "name": name,
            "description": description or f"The {name} field.",
            "range": range_type,
        }
        if required:
            slot["required"] = True
        self.slots[name] = slot

    def _add_enum(self, name, values, description=""):
        if name in self.enums:
            return
        pvs = {}
        for v in values:
            val = v["value"]
            if val:
                pvs[val] = {"text": val}
                if v.get("description"):
                    pvs[val]["description"] = v["description"]
        if pvs:
            self.enums[name] = {
                "name": name,
                "description": description,
                "permissible_values": pvs,
            }

    def _process_simple_type_ref(self, type_name, slot_name, description, required):
        if type_name not in self.simple_types:
            self._add_slot(slot_name, description, "string", required)
            return
        st_elem = self.simple_types[type_name]
        enum_values = _xsd_extract_inline_enum(st_elem)
        if enum_values:
            enum_name = _make_enum_name(slot_name)
            self._add_enum(enum_name, enum_values, _xsd_get_doc(st_elem))
            self._add_slot(slot_name, description, enum_name, required)
        else:
            restriction = st_elem.find("xs:restriction", NS)
            base = restriction.get("base") if restriction is not None else "xs:string"
            range_type = _xsd_get_linkml_range(base)
            self._add_slot(slot_name, description, range_type, required)

    def _process_attribute(self, attr_elem, force_optional=False):
        name = attr_elem.get("name")
        if not name:
            return
        description = _xsd_get_doc(attr_elem)
        use = attr_elem.get("use", "optional")
        required = (use == "required") and not force_optional
        type_ref = attr_elem.get("type")

        inline_st = attr_elem.find("xs:simpleType", NS)
        if inline_st is not None:
            enum_values = _xsd_extract_inline_enum(inline_st)
            if enum_values:
                enum_name = _make_enum_name(name)
                self._add_enum(enum_name, enum_values, description)
                self._add_slot(name, description, enum_name, required)
                return

        if type_ref:
            if type_ref in self.simple_types:
                self._process_simple_type_ref(type_ref, name, description, required)
            else:
                range_type = _xsd_get_linkml_range(type_ref)
                self._add_slot(name, description, range_type, required)
        else:
            self._add_slot(name, description, "string", required)

    def _process_choice_as_enum(self, choice_elem, parent_name, parent_desc, required):
        options = []
        has_complex_children = False

        for child in choice_elem:
            tag = child.tag.replace(f"{{{XS_NS}}}", "xs:")
            if tag == "xs:element":
                child_name = child.get("name")
                if child_name:
                    desc = _xsd_get_doc(child)
                    options.append({"value": child_name, "description": desc})
                    inline_ct = child.find("xs:complexType", NS)
                    if inline_ct is not None:
                        if (inline_ct.findall(".//xs:attribute", NS) or
                            inline_ct.findall(".//xs:element", NS)):
                            has_complex_children = True

        if options and not has_complex_children:
            enum_name = _make_enum_name(parent_name)
            self._add_enum(enum_name, options, parent_desc)
            self._add_slot(parent_name, parent_desc, enum_name, required)
        elif options:
            enum_name = _make_enum_name(parent_name)
            self._add_enum(enum_name, options, parent_desc)
            self._add_slot(parent_name, parent_desc, enum_name, required)
            for child in choice_elem:
                tag = child.tag.replace(f"{{{XS_NS}}}", "xs:")
                if tag == "xs:element":
                    inline_ct = child.find("xs:complexType", NS)
                    if inline_ct is not None:
                        for attr in inline_ct.findall(".//xs:attribute", NS):
                            self._process_attribute(attr, force_optional=True)

    def walk_complex_type(self, ct_elem, force_optional=False):
        if ct_elem is None:
            return

        content = ct_elem.find("xs:complexContent", NS)
        if content is not None:
            extension = content.find("xs:extension", NS)
            if extension is not None:
                base = extension.get("base")
                if base and not self._should_skip_type(base):
                    if base in self.complex_types:
                        self.walk_complex_type(self.complex_types[base], force_optional)
                ct_elem = extension

        for attr in ct_elem.findall("xs:attribute", NS):
            self._process_attribute(attr, force_optional)

        for container_type in ["xs:sequence", "xs:all", "xs:choice"]:
            for container in ct_elem.findall(f".//{container_type}", NS):
                is_choice = container_type == "xs:choice"
                self._walk_container(container, force_optional or is_choice)

    def _walk_container(self, container, force_optional=False):
        for child in container:
            tag = child.tag.replace(f"{{{XS_NS}}}", "xs:")
            if tag == "xs:element":
                self._process_element(child, force_optional)
            elif tag == "xs:choice":
                self._walk_container(child, force_optional=True)
            elif tag in ("xs:sequence", "xs:all"):
                self._walk_container(child, force_optional)
            elif tag == "xs:attribute":
                self._process_attribute(child, force_optional)

    def _process_element(self, elem, force_optional=False):
        name = elem.get("name")
        if not name:
            return
        if self._should_skip_element(name):
            return

        description = _xsd_get_doc(elem)
        min_occurs = elem.get("minOccurs", "1")
        required = (min_occurs != "0") and not force_optional
        type_ref = elem.get("type")

        if type_ref and self._should_skip_type(type_ref):
            return

        if type_ref and type_ref in self.simple_types:
            self._process_simple_type_ref(type_ref, name, description, required)
            return

        if type_ref and _xsd_is_primitive_type(type_ref):
            range_type = _xsd_get_linkml_range(type_ref)
            self._add_slot(name, description, range_type, required)
            return

        if type_ref and type_ref in self.complex_types:
            self.walk_complex_type(self.complex_types[type_ref], not required)
            return

        if type_ref and type_ref.startswith("com:"):
            if type_ref == "com:RefObjectType":
                self._add_slot(name, description, "string", required)
            return

        inline_ct = elem.find("xs:complexType", NS)
        if inline_ct is not None:
            self._process_inline_complex_type(inline_ct, name, description, required)
            return

        inline_st = elem.find("xs:simpleType", NS)
        if inline_st is not None:
            enum_values = _xsd_extract_inline_enum(inline_st)
            if enum_values:
                enum_name = _make_enum_name(name)
                self._add_enum(enum_name, enum_values, description)
                self._add_slot(name, description, enum_name, required)
            else:
                self._add_slot(name, description, "string", required)
            return

        if type_ref is None:
            self._add_slot(name, description, "string", required)

    def _process_inline_complex_type(self, ct_elem, parent_name, parent_desc, required):
        has_direct_attributes = bool(ct_elem.findall("xs:attribute", NS))
        choice = ct_elem.find("xs:choice", NS)
        sequence = ct_elem.find("xs:sequence", NS)
        all_container = ct_elem.find("xs:all", NS)

        if choice is not None:
            choice_elements = choice.findall("xs:element", NS)
            if choice_elements:
                self._process_choice_as_enum(choice, parent_name, parent_desc, required)
                return

        if sequence is not None or all_container is not None:
            self.walk_complex_type(ct_elem, not required)
            return

        if has_direct_attributes:
            for attr in ct_elem.findall("xs:attribute", NS):
                self._process_attribute(attr, not required)
            return

        self._add_slot(parent_name, parent_desc, "string", required)


def convert_xsd_to_linkml(filepath, base_uri):
    """Convert an XSD file to a LinkML schema dict."""
    _root, complex_types, simple_types = _xsd_parse(filepath)

    type_name, main_type = _xsd_find_main_type(complex_types, filepath)
    if main_type is None:
        return None

    walker = _XSDWalker(complex_types, simple_types)
    walker.walk_complex_type(main_type)

    basename = os.path.basename(filepath)
    schema_name = os.path.splitext(basename)[0].replace(".", "_")
    title = type_name.replace("Type", "") if type_name else schema_name
    description = _xsd_get_doc(main_type) or f"Schema derived from {basename}"
    schema_id = base_uri.rstrip("/") + "/" + schema_name

    schema = {
        "id": schema_id,
        "name": schema_name,
        "title": title,
        "description": description,
        "version": "1.0.0",
        "imports": ["linkml:types"],
        "prefixes": {
            "linkml": "https://w3id.org/linkml/",
            "ENA": "https://www.ebi.ac.uk/ena/browser/view/",
        },
        "default_range": "string",
    }

    slot_names = list(walker.slots.keys())
    slot_usage = {}
    for i, name in enumerate(slot_names, 1):
        slot_usage[name] = {"rank": i}

    main_class = {
        "name": schema_name,
        "title": title,
        "description": description,
        "is_a": "dh_interface",
        "slots": slot_names,
        "slot_usage": slot_usage,
    }

    schema["classes"] = {
        "dh_interface": {
            "name": "dh_interface",
            "description": "A DataHarmonizer interface",
            "from_schema": schema_id,
        },
        schema_name: main_class,
    }

    schema["slots"] = walker.slots

    if walker.enums:
        schema["enums"] = walker.enums

    return schema


# ---------------------------------------------------------------------------
# Merging (from merge_linkml.py)
# ---------------------------------------------------------------------------

def _get_main_class(schema):
    """Return (name, class_dict) for the main class (is_a dh_interface)."""
    for name, cls in schema.get("classes", {}).items():
        if isinstance(cls, dict) and cls.get("is_a") == "dh_interface":
            return name, cls
    return None, None


def _ordered_slot_names(schema):
    """Return the slot names listed in the main class, preserving order."""
    _, main_cls = _get_main_class(schema)
    if main_cls is None:
        return []
    return list(main_cls.get("slots", []))


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
    """
    if not schemas:
        return {}

    merged_slots = {}
    merged_enums = {}
    merged_slot_usage = {}
    seen_slot_order = []

    for idx, schema in enumerate(schemas):
        source_prefix = source_names[idx] if source_names and idx < len(source_names) else None

        for slot_name in _ordered_slot_names(schema):
            if slot_name not in merged_slots:
                seen_slot_order.append(slot_name)

            slots = schema.get("slots", {})
            if slot_name not in merged_slots and slot_name in slots:
                slot_def = dict(slots[slot_name])
                if source_prefix:
                    slot_def["source"] = source_prefix
                merged_slots[slot_name] = slot_def

            _, main_cls = _get_main_class(schema)
            if main_cls and slot_name not in merged_slot_usage:
                su = main_cls.get("slot_usage", {})
                if slot_name in su:
                    usage = dict(su[slot_name])
                    if source_prefix and "slot_group" in usage:
                        usage["slot_group"] = source_prefix + ":" + usage["slot_group"]
                    merged_slot_usage[slot_name] = usage

        for enum_name, enum_def in schema.get("enums", {}).items():
            if enum_name not in merged_enums:
                merged_enums[enum_name] = enum_def

    for idx, schema in enumerate(schemas):
        source_prefix = source_names[idx] if source_names and idx < len(source_names) else None
        for slot_name, slot_def in schema.get("slots", {}).items():
            if slot_name not in merged_slots:
                slot_def = dict(slot_def)
                if source_prefix:
                    slot_def["source"] = source_prefix
                merged_slots[slot_name] = slot_def
                seen_slot_order.append(slot_name)

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
    """Assemble a complete LinkML schema dict from merge results and metadata."""
    first = schemas[0]

    if name is None:
        name = first.get("name", "merged")
    if title is None:
        title = first.get("title", name)
    if description is None:
        description = first.get("description", "")

    if base_uri is None:
        first_id = first.get("id", "")
        base_uri = first_id.rsplit("/", 1)[0] if "/" in first_id else first_id

    schema_id = base_uri.rstrip("/") + "/" + name

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
# Filtering (from filter_linkml.py)
# ---------------------------------------------------------------------------

def load_field_list(filepath):
    """Read a newline-separated text file of field names."""
    with open(filepath, "r", encoding="utf-8") as f:
        names = []
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                names.append(line)
        return names


def _referenced_enums(slots_dict):
    """Return the set of enum names referenced by slots' range."""
    refs = set()
    for slot_def in slots_dict.values():
        r = slot_def.get("range", "")
        if r:
            refs.add(r)
    return refs


def filter_schema(schema, include=None, exclude=None):
    """Filter slots in schema and return a new schema dict."""
    main_name, main_cls = _get_main_class(schema)
    if main_cls is None:
        print("Warning: no main class (is_a: dh_interface) found in schema", file=sys.stderr)
        return schema

    all_slot_names = list(main_cls.get("slots", []))
    all_slot_set = set(all_slot_names)

    if include is not None:
        unknown = [n for n in include if n not in all_slot_set]
        if unknown:
            print(f"Warning: include list contains {len(unknown)} field(s) not in schema: "
                  f"{', '.join(unknown[:5])}{'...' if len(unknown) > 5 else ''}", file=sys.stderr)

    if exclude is not None:
        unknown = [n for n in exclude if n not in all_slot_set]
        if unknown:
            print(f"Warning: exclude list contains {len(unknown)} field(s) not in schema: "
                  f"{', '.join(unknown[:5])}{'...' if len(unknown) > 5 else ''}", file=sys.stderr)

    if include is not None:
        include_set = set(include)
        kept = [s for s in all_slot_names if s in include_set]
    else:
        kept = list(all_slot_names)

    if exclude is not None:
        exclude_set = set(exclude)
        kept = [s for s in kept if s not in exclude_set]

    filtered = {}
    for key, value in schema.items():
        if key in ("classes", "slots", "enums"):
            continue
        filtered[key] = value

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

    old_slots = schema.get("slots", {})
    new_slots = {name: old_slots[name] for name in kept if name in old_slots}
    filtered["slots"] = new_slots

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
        "input_files",
        nargs="+",
        help=(
            "Input files to process. Can be any mix of: "
            ".xsd (ENA/SRA schema), .xml (ENA checklist), .yaml/.yml (LinkML). "
            "Order determines merge priority (first file = highest priority)."
        ),
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
        help="Schema name for the output (default: from highest-priority input).",
    )
    parser.add_argument(
        "--title",
        default=None,
        help="Schema title for the output (default: from highest-priority input).",
    )
    parser.add_argument(
        "--description",
        default=None,
        help="Schema description for the output (default: from highest-priority input).",
    )
    parser.add_argument(
        "--base-uri",
        default="https://github.com/timrozday/ena-submission-dataharmonizer",
        help="Base URI for the schema id (default: %(default)s).",
    )

    args = parser.parse_args()

    # -- validate: all input files exist ------------------------------------
    all_files = list(args.input_files)
    if args.include:
        all_files.append(args.include)
    if args.exclude:
        all_files.append(args.exclude)

    for path in all_files:
        if not os.path.isfile(path):
            print(f"Error: file not found: {path}", file=sys.stderr)
            sys.exit(1)

    # -- step 1: convert all inputs to LinkML dicts -------------------------
    schemas = []
    print(f"Processing {len(args.input_files)} input file(s)...")

    for filepath in args.input_files:
        file_type = detect_file_type(filepath)
        print(f"  [{file_type or 'unknown'}] {filepath}")

        if file_type == "linkml":
            schema = load_linkml(filepath)
        elif file_type == "xsd":
            schema = convert_xsd_to_linkml(filepath, args.base_uri)
            if schema is None:
                print(f"    Warning: no suitable complexType found, skipping")
                continue
        elif file_type == "xml":
            schema = convert_ena_xml_to_linkml(filepath, args.base_uri)
        else:
            print(f"    Warning: unknown file type, skipping")
            continue

        if schema:
            n_slots = len(schema.get("slots", {}))
            n_enums = len(schema.get("enums", {}))
            print(f"    -> {n_slots} slots, {n_enums} enums")
            schemas.append(schema)

    if not schemas:
        print("Error: no valid schemas to merge", file=sys.stderr)
        sys.exit(1)

    # -- step 2: merge all schemas ------------------------------------------
    print(f"\nMerging {len(schemas)} schema(s)...")
    source_names = [os.path.splitext(os.path.basename(p))[0] for p in args.input_files[:len(schemas)]]
    merge_result = merge_schemas(schemas, source_names=source_names)
    schema = build_merged_schema(
        merge_result,
        schemas,
        name=args.name,
        title=args.title,
        description=args.description,
        base_uri=args.base_uri,
    )

    # -- step 3: filter (optional) ------------------------------------------
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

    # -- step 4: write ------------------------------------------------------
    write_yaml(schema, args.output)
    print(f"\nWritten: {args.output}")

    # -- summary ------------------------------------------------------------
    n_slots = len(schema.get("slots", {}))
    n_required = sum(1 for s in schema.get("slots", {}).values() if s.get("required"))
    n_enums = len(schema.get("enums", {}))
    print(f"  {n_slots} slots ({n_required} required), {n_enums} enums")
    print("Done.")


if __name__ == "__main__":
    main()
