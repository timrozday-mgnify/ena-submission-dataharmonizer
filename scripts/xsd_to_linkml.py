#!/usr/bin/env python3
"""Convert ENA/SRA XSD schema files to DataHarmonizer-compatible LinkML YAML schemas.

Parses XSD files (e.g. SRA.study.xsd, ENA.project.xsd) using a recursive walker
to extract fields, attributes, and enumerations from complex types.

Usage:
    python scripts/xsd_to_linkml.py
    python scripts/xsd_to_linkml.py assets/ena_schema/SRA.study.xsd
    python scripts/xsd_to_linkml.py -i assets/ena_schema/ -o schemas/
"""

import argparse
import os
import re
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
# XSD namespace constants
# ---------------------------------------------------------------------------

XS_NS = "http://www.w3.org/2001/XMLSchema"
NS = {"xs": XS_NS}

# Patterns and types to skip
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

# XSD primitive types that map to LinkML ranges
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


# ---------------------------------------------------------------------------
# XSD parsing
# ---------------------------------------------------------------------------

def _get_doc(elem):
    """Extract documentation text from xs:annotation/xs:documentation."""
    ann = elem.find("xs:annotation", NS)
    if ann is not None:
        doc = ann.find("xs:documentation", NS)
        if doc is not None and doc.text:
            return " ".join(doc.text.split())
    return ""


def _is_primitive_type(type_name):
    """Check if a type is an XSD primitive or maps to a LinkML primitive."""
    if type_name is None:
        return False
    return type_name.startswith("xs:") or type_name in XSD_TO_LINKML_TYPE


def _get_linkml_range(xsd_type):
    """Map an XSD type to a LinkML range."""
    if xsd_type is None:
        return "string"
    return XSD_TO_LINKML_TYPE.get(xsd_type, "string")


def _make_enum_name(field_name):
    """Convert field name to PascalCaseMenu enum name."""
    # Handle names with underscores
    parts = field_name.replace("-", "_").split("_")
    return "".join(p.capitalize() for p in parts) + "Menu"


def _extract_inline_enum(simple_type_elem):
    """Extract enumeration values from an inline xs:simpleType."""
    restriction = simple_type_elem.find("xs:restriction", NS)
    if restriction is None:
        return None

    enum_values = []
    for enum_elem in restriction.findall("xs:enumeration", NS):
        val = enum_elem.get("value")
        if val is not None:
            desc = _get_doc(enum_elem)
            enum_values.append({"value": val, "description": desc})

    return enum_values if enum_values else None


def parse_xsd(filepath):
    """Parse an XSD file and return the root element and named types.

    Returns
    -------
    tuple: (root_element, named_complex_types, named_simple_types)
        - named_complex_types: dict mapping type name to element
        - named_simple_types: dict mapping type name to element
    """
    tree = ET.parse(filepath)
    root = tree.getroot()

    # Build lookup dicts for named types
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


def _find_main_type(complex_types, filename):
    """Find the main complexType to process based on filename conventions.

    For SRA.study.xsd -> StudyType
    For ENA.project.xsd -> ProjectType
    For SRA.experiment.xsd -> ExperimentType
    For SRA.run.xsd -> RunType
    """
    basename = os.path.basename(filename).lower()

    # Map filename patterns to expected type names
    patterns = [
        ("study", "StudyType"),
        ("project", "ProjectType"),
        ("experiment", "ExperimentType"),
        ("run", "RunType"),
    ]

    for pattern, type_name in patterns:
        if pattern in basename and type_name in complex_types:
            return type_name, complex_types[type_name]

    # Fallback: look for *Type that isn't *SetType
    for name, elem in complex_types.items():
        if name.endswith("Type") and not name.endswith("SetType"):
            return name, elem

    return None, None


class XSDWalker:
    """Recursive walker that extracts fields from XSD complex types."""

    def __init__(self, complex_types, simple_types):
        self.complex_types = complex_types
        self.simple_types = simple_types
        self.slots = {}
        self.enums = {}
        self.seen_names = set()
        self._rank = 1

    def _should_skip_element(self, name):
        """Check if element should be skipped based on name patterns."""
        return SKIP_ELEMENT_PATTERNS.match(name) is not None

    def _should_skip_type(self, type_ref):
        """Check if type reference should be skipped."""
        return type_ref in SKIP_COM_TYPES

    def _add_slot(self, name, description, range_type="string", required=False):
        """Add a slot if not already seen (first-seen-wins deduplication)."""
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
        self._rank += 1

    def _add_enum(self, name, values, description=""):
        """Add an enum definition."""
        if name in self.enums:
            return

        pvs = {}
        for v in values:
            val = v["value"]
            if val:  # Skip empty values
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
        """Process a reference to a named simpleType."""
        if type_name not in self.simple_types:
            self._add_slot(slot_name, description, "string", required)
            return

        st_elem = self.simple_types[type_name]
        enum_values = _extract_inline_enum(st_elem)

        if enum_values:
            enum_name = _make_enum_name(slot_name)
            self._add_enum(enum_name, enum_values, _get_doc(st_elem))
            self._add_slot(slot_name, description, enum_name, required)
        else:
            # Check base type
            restriction = st_elem.find("xs:restriction", NS)
            base = restriction.get("base") if restriction is not None else "xs:string"
            range_type = _get_linkml_range(base)
            self._add_slot(slot_name, description, range_type, required)

    def _process_attribute(self, attr_elem, force_optional=False):
        """Process an xs:attribute element."""
        name = attr_elem.get("name")
        if not name:
            return

        description = _get_doc(attr_elem)
        use = attr_elem.get("use", "optional")
        required = (use == "required") and not force_optional

        type_ref = attr_elem.get("type")

        # Check for inline simpleType with enum
        inline_st = attr_elem.find("xs:simpleType", NS)
        if inline_st is not None:
            enum_values = _extract_inline_enum(inline_st)
            if enum_values:
                enum_name = _make_enum_name(name)
                self._add_enum(enum_name, enum_values, description)
                self._add_slot(name, description, enum_name, required)
                return

        if type_ref:
            if type_ref in self.simple_types:
                self._process_simple_type_ref(type_ref, name, description, required)
            else:
                range_type = _get_linkml_range(type_ref)
                self._add_slot(name, description, range_type, required)
        else:
            self._add_slot(name, description, "string", required)

    def _process_choice_as_enum(self, choice_elem, parent_name, parent_desc, required):
        """Process xs:choice where children are empty types -> treat as enum."""
        # Collect choice options
        options = []
        has_complex_children = False

        for child in choice_elem:
            tag = child.tag.replace(f"{{{XS_NS}}}", "xs:")
            if tag == "xs:element":
                child_name = child.get("name")
                if child_name:
                    desc = _get_doc(child)
                    options.append({"value": child_name, "description": desc})

                    # Check if this choice option has meaningful content
                    inline_ct = child.find("xs:complexType", NS)
                    if inline_ct is not None:
                        # Check for attributes or child elements
                        if (inline_ct.findall(".//xs:attribute", NS) or
                            inline_ct.findall(".//xs:element", NS)):
                            has_complex_children = True

        # Create enum for the choice itself
        if options and not has_complex_children:
            enum_name = _make_enum_name(parent_name)
            self._add_enum(enum_name, options, parent_desc)
            self._add_slot(parent_name, parent_desc, enum_name, required)
        elif options:
            # Choice has complex children - still create enum but also recurse
            enum_name = _make_enum_name(parent_name)
            self._add_enum(enum_name, options, parent_desc)
            self._add_slot(parent_name, parent_desc, enum_name, required)

            # Recurse into choice children for their attributes
            for child in choice_elem:
                tag = child.tag.replace(f"{{{XS_NS}}}", "xs:")
                if tag == "xs:element":
                    inline_ct = child.find("xs:complexType", NS)
                    if inline_ct is not None:
                        # Process attributes from choice options
                        for attr in inline_ct.findall(".//xs:attribute", NS):
                            self._process_attribute(attr, force_optional=True)

    def walk_complex_type(self, ct_elem, force_optional=False):
        """Recursively walk a complexType element extracting fields."""
        if ct_elem is None:
            return

        # Handle extension base
        content = ct_elem.find("xs:complexContent", NS)
        if content is not None:
            extension = content.find("xs:extension", NS)
            if extension is not None:
                base = extension.get("base")
                # Skip com: types but note RefObjectType as a string reference
                if base and not self._should_skip_type(base):
                    if base in self.complex_types:
                        self.walk_complex_type(self.complex_types[base], force_optional)
                # Continue with extension content
                ct_elem = extension

        # Process direct attributes
        for attr in ct_elem.findall("xs:attribute", NS):
            self._process_attribute(attr, force_optional)

        # Process all child containers (sequence, all, choice)
        for container_type in ["xs:sequence", "xs:all", "xs:choice"]:
            for container in ct_elem.findall(f".//{container_type}", NS):
                is_choice = container_type == "xs:choice"
                self._walk_container(container, force_optional or is_choice)

    def _walk_container(self, container, force_optional=False):
        """Walk a sequence, all, or choice container."""
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
        """Process an xs:element within a container."""
        name = elem.get("name")
        if not name:
            return

        # Skip patterns
        if self._should_skip_element(name):
            return

        description = _get_doc(elem)
        min_occurs = elem.get("minOccurs", "1")
        required = (min_occurs != "0") and not force_optional
        type_ref = elem.get("type")

        # Check if type should be skipped
        if type_ref and self._should_skip_type(type_ref):
            return

        # Case 1: Reference to named simpleType
        if type_ref and type_ref in self.simple_types:
            self._process_simple_type_ref(type_ref, name, description, required)
            return

        # Case 2: Primitive XSD type
        if type_ref and _is_primitive_type(type_ref):
            range_type = _get_linkml_range(type_ref)
            self._add_slot(name, description, range_type, required)
            return

        # Case 3: Reference to named complexType
        if type_ref and type_ref in self.complex_types:
            self.walk_complex_type(self.complex_types[type_ref], not required)
            return

        # Case 4: External reference (com:RefObjectType etc.) -> string
        if type_ref and type_ref.startswith("com:"):
            if type_ref == "com:RefObjectType":
                # Reference fields become string slots
                self._add_slot(name, description, "string", required)
            return

        # Case 5: Inline complexType
        inline_ct = elem.find("xs:complexType", NS)
        if inline_ct is not None:
            self._process_inline_complex_type(inline_ct, name, description, required)
            return

        # Case 6: Inline simpleType
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

        # Default: string field
        if type_ref is None:
            self._add_slot(name, description, "string", required)

    def _process_inline_complex_type(self, ct_elem, parent_name, parent_desc, required):
        """Process an inline complexType within an element."""
        # Check what's inside - look for direct child containers
        has_direct_attributes = bool(ct_elem.findall("xs:attribute", NS))
        choice = ct_elem.find("xs:choice", NS)
        sequence = ct_elem.find("xs:sequence", NS)
        all_container = ct_elem.find("xs:all", NS)

        # Case: Direct xs:choice (choice-as-enum pattern)
        # This handles LIBRARY_LAYOUT with SINGLE/PAIRED choice
        if choice is not None:
            choice_elements = choice.findall("xs:element", NS)
            if choice_elements:
                self._process_choice_as_enum(choice, parent_name, parent_desc, required)
                return

        # Case: Has sequence or all container with elements -> recurse
        if sequence is not None or all_container is not None:
            self.walk_complex_type(ct_elem, not required)
            return

        # Case: Only attributes, no child containers -> emit as slots
        if has_direct_attributes:
            for attr in ct_elem.findall("xs:attribute", NS):
                self._process_attribute(attr, not required)
            return

        # Default: treat as string
        self._add_slot(parent_name, parent_desc, "string", required)


# ---------------------------------------------------------------------------
# LinkML conversion
# ---------------------------------------------------------------------------

def convert_xsd_to_linkml(filepath, base_uri):
    """Convert an XSD file to a LinkML schema dict.

    Parameters
    ----------
    filepath : str
        Path to the XSD file.
    base_uri : str
        Base URI for the schema id.

    Returns
    -------
    dict suitable for YAML serialisation as a LinkML schema, or None if no
    suitable complexType found.
    """
    _root, complex_types, simple_types = parse_xsd(filepath)

    # Find main type to process
    type_name, main_type = _find_main_type(complex_types, filepath)
    if main_type is None:
        return None

    # Walk the schema
    walker = XSDWalker(complex_types, simple_types)
    walker.walk_complex_type(main_type)

    # Build schema name from filename
    basename = os.path.basename(filepath)
    schema_name = os.path.splitext(basename)[0].replace(".", "_")

    # Extract title from type name
    title = type_name.replace("Type", "") if type_name else schema_name

    # Get description from main type
    description = _get_doc(main_type) or f"Schema derived from {basename}"

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

    # Build slot list and slot_usage
    slot_names = list(walker.slots.keys())
    slot_usage = {}
    for i, name in enumerate(slot_names, 1):
        slot_usage[name] = {"rank": i}

    # Classes
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
        description="Convert ENA/SRA XSD schema files to DataHarmonizer LinkML YAML.",
    )
    parser.add_argument(
        "input_files",
        nargs="*",
        help="XSD file(s) to convert. If omitted, all *.xsd (except SRA.common.xsd) in --input-dir are processed.",
    )
    parser.add_argument(
        "-i", "--input-dir",
        default="assets/ena_schema",
        help="Directory containing XSD files (default: assets/ena_schema).",
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
        xsd_files = args.input_files
    else:
        xsd_files = sorted(
            os.path.join(args.input_dir, f)
            for f in os.listdir(args.input_dir)
            if f.lower().endswith(".xsd") and f.lower() != "sra.common.xsd"
        )

    if not xsd_files:
        print(f"No XSD files found in {args.input_dir}")
        return

    print(f"Processing {len(xsd_files)} file(s)...")
    for xsd_path in xsd_files:
        print(f"\n  Parsing: {xsd_path}")
        schema = convert_xsd_to_linkml(xsd_path, args.base_uri)

        if schema is None:
            print(f"  Skipped: no suitable complexType found")
            continue

        out_name = schema["name"] + ".yaml"
        out_path = os.path.join(args.output_dir, out_name)
        write_yaml(schema, out_path)
        print(f"  Written: {out_path}")

        # Summary
        n_slots = len(schema["slots"])
        n_required = sum(1 for s in schema["slots"].values() if s.get("required"))
        n_enums = len(schema.get("enums", {}))
        print(f"  Fields: {n_slots} ({n_required} required), Enums: {n_enums}")

    print("\nDone.")


if __name__ == "__main__":
    main()
