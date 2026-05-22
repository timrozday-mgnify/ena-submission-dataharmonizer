"""Load and write LinkML schemas.

Public functions:
    load_yaml(path) -> dict          # parse an existing LinkML YAML file
    load_xml(path, base_uri) -> dict # convert ENA checklist XML to LinkML
    load_xsd(path, base_uri) -> dict # convert XSD to LinkML
    load_any(path, base_uri) -> dict # dispatch by extension (.yaml/.yml/.xml/.xsd)
    write_yaml(schema, path)         # write LinkML schema dict to YAML
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from ._yaml_dump import LinkMLDumper


DEFAULT_BASE_URI = "https://github.com/timrozday/ena-submission-dataharmonizer"


def load_yaml(path: str | Path) -> dict[str, Any]:
    """Load a LinkML YAML schema file and return the parsed dict."""
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_xml(path: str | Path, base_uri: str = DEFAULT_BASE_URI) -> dict[str, Any] | None:
    """Convert an ENA checklist XML file to a LinkML schema dict."""
    from . import convert_xml
    return convert_xml.from_path(path, base_uri)


def load_xsd(path: str | Path, base_uri: str = DEFAULT_BASE_URI) -> dict[str, Any] | None:
    """Convert an XSD schema file to a LinkML schema dict."""
    from . import convert_xsd
    return convert_xsd.from_path(path, base_uri)


def load_any(path: str | Path, base_uri: str = DEFAULT_BASE_URI) -> dict[str, Any] | None:
    """Dispatch to the right loader by file extension. Returns None if unsupported."""
    ext = os.path.splitext(str(path))[1].lower()
    if ext in (".yaml", ".yml"):
        return load_yaml(path)
    if ext == ".xsd":
        return load_xsd(path, base_uri)
    if ext == ".xml":
        return load_xml(path, base_uri)
    return None


def write_yaml(schema: dict[str, Any], path: str | Path) -> None:
    """Write a LinkML schema dict to a YAML file with consistent style."""
    parent = os.path.dirname(str(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        yaml.dump(schema, fh, Dumper=LinkMLDumper, default_flow_style=False,
                  sort_keys=False, allow_unicode=True, width=120)
