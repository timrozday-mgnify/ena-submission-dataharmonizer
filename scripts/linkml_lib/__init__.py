"""LinkML schema library for the DataHarmonizer / ENA submission pipeline.

Submodules:
    io          load_yaml, load_xml, load_xsd, load_any, write_yaml
    convert_xml ENA checklist XML → schema dict (from_path, parse_checklist, to_linkml)
    convert_xsd XSD → schema dict (from_path, XSDWalker)
    schema      get_main_class, slot_meta, summary, diff, slot/title maps
    transform   merge, filter, load_field_list
    pipeline    build (convert → merge → filter)
    dh_data     filter_columns, remap_titles_to_names, validate

The unified CLI is ``scripts/dh_schema.py``.
"""

from __future__ import annotations

from . import convert_xml, convert_xsd, dh_data, io, pipeline, schema, transform
from .io import DEFAULT_BASE_URI, load_any, load_xml, load_xsd, load_yaml, write_yaml

__all__ = [
    "convert_xml", "convert_xsd", "dh_data", "io", "pipeline", "schema", "transform",
    "DEFAULT_BASE_URI", "load_any", "load_xml", "load_xsd", "load_yaml", "write_yaml",
]
