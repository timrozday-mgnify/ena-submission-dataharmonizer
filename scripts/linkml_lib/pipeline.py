"""Full schema-building pipeline: convert → merge → filter.

Public function:
    build(inputs, *, base_uri, name, title, description, include, exclude) -> dict
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Sequence

from . import io
from .io import DEFAULT_BASE_URI
from .transform import filter as filter_schema, merge


def build(
    inputs: Sequence[str | Path],
    *,
    base_uri: str = DEFAULT_BASE_URI,
    name: str | None = None,
    title: str | None = None,
    description: str | None = None,
    include: Sequence[str] | None = None,
    exclude: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Convert each input file, merge them (priority = input order), then optionally filter.

    Inputs are auto-detected by extension (.yaml/.yml/.xml/.xsd). Files that fail
    to convert are silently skipped — at least one must produce a schema or
    ValueError is raised.
    """
    paths = [str(p) for p in inputs]
    schemas = [s for p in paths if (s := io.load_any(p, base_uri)) is not None]

    if not schemas:
        raise ValueError("No valid schemas found in input files")

    source_names = [os.path.splitext(os.path.basename(p))[0] for p in paths[:len(schemas)]]
    schema = merge(
        schemas,
        source_names=source_names,
        name=name, title=title, description=description, base_uri=base_uri,
    )

    if include is not None or exclude is not None:
        schema = filter_schema(schema, include=include, exclude=exclude)

    return schema
