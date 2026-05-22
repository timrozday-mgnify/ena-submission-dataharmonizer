"""Convert ENA/SRA XSD schema files to LinkML schema dicts.

Public functions:
    from_path(path, base_uri) -> dict | None
    convert(path, base_uri) -> dict | None       # alias for from_path

XSDWalker is exported for advanced use cases (e.g. custom main-type selection).
"""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


XS_NS = "http://www.w3.org/2001/XMLSchema"
NS = {"xs": XS_NS}

SKIP_ELEMENT_PATTERNS = re.compile(r"^(.*_LINKS|.*_ATTRIBUTES|RELATED_.*)$", re.IGNORECASE)
SKIP_COM_TYPES = {
    "com:LinkType", "com:AttributeType", "com:SpotDescriptorType",
    "com:ProcessingType", "com:ReferenceSequenceType", "com:XRefType", "com:PlatformType",
}

XSD_TO_LINKML_TYPE = {
    "xs:string": "string", "xs:int": "integer", "xs:integer": "integer",
    "xs:nonNegativeInteger": "integer", "xs:positiveInteger": "integer",
    "xs:float": "float", "xs:double": "float", "xs:decimal": "float",
    "xs:boolean": "boolean", "xs:date": "date", "xs:dateTime": "datetime",
    "xs:token": "string",
}

# Filename → expected main complexType name.
MAIN_TYPE_PATTERNS = (
    ("study", "StudyType"),
    ("project", "ProjectType"),
    ("experiment", "ExperimentType"),
    ("run", "RunType"),
)


def from_path(path: str | Path, base_uri: str) -> dict[str, Any] | None:
    """Parse an XSD file and convert it to a LinkML schema dict.

    Returns None if no suitable main complexType is found.
    """
    _, complex_types, simple_types = _parse(path)
    type_name, main_type = _find_main_type(complex_types, str(path))
    if main_type is None:
        return None

    walker = XSDWalker(complex_types, simple_types)
    walker.walk_complex_type(main_type)

    basename = os.path.basename(str(path))
    schema_name = os.path.splitext(basename)[0].replace(".", "_")
    title = type_name.replace("Type", "") if type_name else schema_name
    description = _get_doc(main_type) or f"Schema derived from {basename}"
    schema_id = base_uri.rstrip("/") + "/" + schema_name

    slot_names = list(walker.slots.keys())
    slot_usage = {name: {"rank": i} for i, name in enumerate(slot_names, 1)}

    schema: dict[str, Any] = {
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
        "classes": {
            "dh_interface": {
                "name": "dh_interface",
                "description": "A DataHarmonizer interface",
                "from_schema": schema_id,
            },
            schema_name: {
                "name": schema_name,
                "title": title,
                "description": description,
                "is_a": "dh_interface",
                "slots": slot_names,
                "slot_usage": slot_usage,
            },
        },
        "slots": walker.slots,
    }
    if walker.enums:
        schema["enums"] = walker.enums
    return schema


convert = from_path  # alias for symmetry with other modules


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _parse(path):
    tree = ET.parse(str(path))
    root = tree.getroot()
    complex_types = {ct.get("name"): ct for ct in root.findall("xs:complexType", NS) if ct.get("name")}
    simple_types = {st.get("name"): st for st in root.findall("xs:simpleType", NS) if st.get("name")}
    return root, complex_types, simple_types


def _find_main_type(complex_types, filename):
    basename = os.path.basename(filename).lower()
    for pattern, type_name in MAIN_TYPE_PATTERNS:
        if pattern in basename and type_name in complex_types:
            return type_name, complex_types[type_name]
    for name, elem in complex_types.items():
        if name.endswith("Type") and not name.endswith("SetType"):
            return name, elem
    return None, None


def _get_doc(elem):
    ann = elem.find("xs:annotation", NS)
    if ann is None:
        return ""
    doc = ann.find("xs:documentation", NS)
    if doc is None or not doc.text:
        return ""
    return " ".join(doc.text.split())


def _is_primitive(type_name):
    return bool(type_name) and (type_name.startswith("xs:") or type_name in XSD_TO_LINKML_TYPE)


def _linkml_range(xsd_type):
    return XSD_TO_LINKML_TYPE.get(xsd_type or "", "string")


def _make_enum_name(field_name):
    parts = field_name.replace("-", "_").split("_")
    return "".join(p.capitalize() for p in parts) + "Menu"


def _extract_inline_enum(simple_type_elem):
    restriction = simple_type_elem.find("xs:restriction", NS)
    if restriction is None:
        return None
    values = [
        {"value": e.get("value"), "description": _get_doc(e)}
        for e in restriction.findall("xs:enumeration", NS)
        if e.get("value") is not None
    ]
    return values or None


# ---------------------------------------------------------------------------
# XSDWalker
# ---------------------------------------------------------------------------

class XSDWalker:
    """Recursive walker that extracts LinkML slots and enums from XSD complex types."""

    def __init__(self, complex_types: dict, simple_types: dict):
        self.complex_types = complex_types
        self.simple_types = simple_types
        self.slots: dict[str, dict] = {}
        self.enums: dict[str, dict] = {}
        self.seen_names: set[str] = set()

    # -- slot/enum bookkeeping --------------------------------------------

    def _add_slot(self, name, description, range_type="string", required=False):
        if name in self.seen_names:
            return
        self.seen_names.add(name)
        slot = {"name": name, "description": description or f"The {name} field.", "range": range_type}
        if required:
            slot["required"] = True
        self.slots[name] = slot

    def _add_enum(self, name, values, description=""):
        if name in self.enums:
            return
        pvs = {}
        for v in values:
            val = v["value"]
            if not val:
                continue
            pvs[val] = {"text": val}
            if v.get("description"):
                pvs[val]["description"] = v["description"]
        if pvs:
            self.enums[name] = {"name": name, "description": description, "permissible_values": pvs}

    # -- type & attribute processing --------------------------------------

    def _process_simple_type_ref(self, type_name, slot_name, description, required):
        st_elem = self.simple_types.get(type_name)
        if st_elem is None:
            self._add_slot(slot_name, description, "string", required)
            return
        enum_values = _extract_inline_enum(st_elem)
        if enum_values:
            enum_name = _make_enum_name(slot_name)
            self._add_enum(enum_name, enum_values, _get_doc(st_elem))
            self._add_slot(slot_name, description, enum_name, required)
            return
        restriction = st_elem.find("xs:restriction", NS)
        base = restriction.get("base") if restriction is not None else "xs:string"
        self._add_slot(slot_name, description, _linkml_range(base), required)

    def _process_attribute(self, attr_elem, force_optional=False):
        name = attr_elem.get("name")
        if not name:
            return
        description = _get_doc(attr_elem)
        required = (attr_elem.get("use", "optional") == "required") and not force_optional
        type_ref = attr_elem.get("type")

        inline_st = attr_elem.find("xs:simpleType", NS)
        if inline_st is not None:
            enum_values = _extract_inline_enum(inline_st)
            if enum_values:
                enum_name = _make_enum_name(name)
                self._add_enum(enum_name, enum_values, description)
                self._add_slot(name, description, enum_name, required)
                return

        if not type_ref:
            self._add_slot(name, description, "string", required)
            return
        if type_ref in self.simple_types:
            self._process_simple_type_ref(type_ref, name, description, required)
            return
        self._add_slot(name, description, _linkml_range(type_ref), required)

    def _process_choice_as_enum(self, choice_elem, parent_name, parent_desc, required):
        options = []
        has_complex_children = False
        for child in choice_elem:
            tag = child.tag.replace(f"{{{XS_NS}}}", "xs:")
            if tag != "xs:element":
                continue
            child_name = child.get("name")
            if not child_name:
                continue
            options.append({"value": child_name, "description": _get_doc(child)})
            inline_ct = child.find("xs:complexType", NS)
            if inline_ct is not None and (
                inline_ct.findall(".//xs:attribute", NS) or inline_ct.findall(".//xs:element", NS)
            ):
                has_complex_children = True

        if not options:
            return

        enum_name = _make_enum_name(parent_name)
        self._add_enum(enum_name, options, parent_desc)
        self._add_slot(parent_name, parent_desc, enum_name, required)

        if has_complex_children:
            for child in choice_elem:
                tag = child.tag.replace(f"{{{XS_NS}}}", "xs:")
                if tag != "xs:element":
                    continue
                inline_ct = child.find("xs:complexType", NS)
                if inline_ct is None:
                    continue
                for attr in inline_ct.findall(".//xs:attribute", NS):
                    self._process_attribute(attr, force_optional=True)

    # -- complexType / container walking ----------------------------------

    def walk_complex_type(self, ct_elem, force_optional=False):
        if ct_elem is None:
            return

        content = ct_elem.find("xs:complexContent", NS)
        if content is not None:
            extension = content.find("xs:extension", NS)
            if extension is not None:
                base = extension.get("base")
                if base and base not in SKIP_COM_TYPES and base in self.complex_types:
                    self.walk_complex_type(self.complex_types[base], force_optional)
                ct_elem = extension

        for attr in ct_elem.findall("xs:attribute", NS):
            self._process_attribute(attr, force_optional)

        for container_type in ("xs:sequence", "xs:all", "xs:choice"):
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
        if not name or SKIP_ELEMENT_PATTERNS.match(name):
            return
        description = _get_doc(elem)
        required = (elem.get("minOccurs", "1") != "0") and not force_optional
        type_ref = elem.get("type")

        if type_ref and type_ref in SKIP_COM_TYPES:
            return
        if type_ref and type_ref in self.simple_types:
            self._process_simple_type_ref(type_ref, name, description, required)
            return
        if type_ref and _is_primitive(type_ref):
            self._add_slot(name, description, _linkml_range(type_ref), required)
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
            enum_values = _extract_inline_enum(inline_st)
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
        choice = ct_elem.find("xs:choice", NS)
        sequence = ct_elem.find("xs:sequence", NS)
        all_container = ct_elem.find("xs:all", NS)
        direct_attributes = ct_elem.findall("xs:attribute", NS)

        if choice is not None and choice.findall("xs:element", NS):
            self._process_choice_as_enum(choice, parent_name, parent_desc, required)
            return
        if sequence is not None or all_container is not None:
            self.walk_complex_type(ct_elem, not required)
            return
        if direct_attributes:
            for attr in direct_attributes:
                self._process_attribute(attr, not required)
            return
        self._add_slot(parent_name, parent_desc, "string", required)
