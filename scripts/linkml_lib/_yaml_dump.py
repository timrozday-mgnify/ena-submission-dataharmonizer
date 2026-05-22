"""YAML dumper that produces DataHarmonizer-compatible LinkML output.

Emits lowercase booleans, uses literal block style for multi-line strings, and
preserves dict insertion order. Used by every writer in ``linkml_lib``.
"""

from __future__ import annotations

import yaml


class LinkMLDumper(yaml.SafeDumper):
    """YAML dumper for LinkML schemas."""
    pass


def _bool_representer(dumper, data):
    return dumper.represent_scalar("tag:yaml.org,2002:bool", "true" if data else "false")


def _str_representer(dumper, data):
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


LinkMLDumper.add_representer(bool, _bool_representer)
LinkMLDumper.add_representer(str, _str_representer)
