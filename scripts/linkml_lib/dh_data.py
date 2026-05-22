"""DataHarmonizer JSON data operations driven by a LinkML schema.

Public functions:
    filter_columns(data, schema, where) -> dict          # SQL WHERE on slot metadata
    remap_titles_to_names(records, schema) -> records    # DH exports use slot titles
    validate(records, schema, *, target_class=None, strict=False) -> ValidationReport

The SQL WHERE in ``filter_columns`` operates on an in-memory SQLite table
``slots`` with columns (name, title, source, required INTEGER 0/1, slot_group,
rank, range). Examples:

    filter_columns(data, schema, "source = 'ENA.sample' OR required = 1")
    filter_columns(data, schema, "name IN ('alias', 'TAXON_ID')")
    filter_columns(data, schema, "source LIKE 'ERC%'")

``validate`` delegates to ``linkml.validator.Validator`` with the
``JsonschemaValidationPlugin`` (covers required slots, type ranges, enums,
patterns, multivalued slots, etc.). Returns the linkml-native
``ValidationReport``; ``report.results`` is a list of ``ValidationResult``
objects with ``severity`` (Severity enum), ``instantiates`` (class name),
``message`` (str), and other fields.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from typing import Any

from linkml.validator import Validator
from linkml.validator.plugins import JsonschemaValidationPlugin
from linkml.validator.report import Severity, ValidationReport, ValidationResult

from .schema import get_main_class, slot_meta, title_to_slot_map


# ---------------------------------------------------------------------------
# Column filtering via SQL WHERE on slot metadata
# ---------------------------------------------------------------------------

def filter_columns(data: dict[str, Any], schema: dict[str, Any], where: str) -> dict[str, Any]:
    """Filter columns of a DataHarmonizer JSON export by SQL WHERE on slot metadata.

    Returns a new ``data`` dict preserving the original ``Container`` structure.
    Raises ValueError for invalid WHERE clauses or malformed DH JSON.
    """
    container = data.get("Container", data)
    if not isinstance(container, dict):
        raise ValueError("Expected JSON with a 'Container' object at the top level")
    container_key = next(iter(container))
    records = container[container_key]
    if not isinstance(records, list):
        raise ValueError(f"Container.{container_key!r} is not a list of records")

    rows = slot_meta(schema)
    selected_names = _select_slot_names(rows, where)
    t2n = title_to_slot_map(schema)
    name_to_title = {v: k for k, v in t2n.items()}
    keep_titles = {name_to_title.get(n, n) for n in selected_names}

    filtered = [{k: v for k, v in row.items() if k in keep_titles} for row in records]
    return {**data, "Container": {container_key: filtered}}


def _select_slot_names(rows: list[dict[str, Any]], where: str) -> set[str]:
    con = sqlite3.connect(":memory:")
    con.execute(
        "CREATE TABLE slots "
        "(name TEXT, title TEXT, source TEXT, required INTEGER, "
        " slot_group TEXT, rank INTEGER, range TEXT)"
    )
    con.executemany("INSERT INTO slots VALUES (?,?,?,?,?,?,?)", [
        (r["name"], r["title"], r["source"], int(r["required"]),
         r["slot_group"], r["rank"], r["range"])
        for r in rows
    ])
    try:
        result = con.execute(f"SELECT name FROM slots WHERE {where}").fetchall()
    except sqlite3.OperationalError as exc:
        raise ValueError(f"Invalid SQL WHERE clause: {exc}") from exc
    return {row[0] for row in result}


# ---------------------------------------------------------------------------
# Title → name remapping
# ---------------------------------------------------------------------------

def remap_titles_to_names(
    records: list[dict[str, Any]],
    schema: dict[str, Any],
) -> list[dict[str, Any]]:
    """Remap record keys from slot titles (DataHarmonizer exports) to slot names."""
    t2n = title_to_slot_map(schema)
    if not t2n:
        return records
    return [{t2n.get(k, k): v for k, v in record.items()} for record in records]


# ---------------------------------------------------------------------------
# Record validation via linkml.validator
# ---------------------------------------------------------------------------

def validate(
    records: Sequence[dict[str, Any]],
    schema: dict[str, Any],
    *,
    target_class: str | None = None,
    strict: bool = False,
) -> ValidationReport:
    """Validate records against a LinkML schema using ``linkml.validator``.

    Parameters
    ----------
    records : sequence of dicts
        Records to validate. Keys should be slot names (use
        ``remap_titles_to_names`` first if you have DataHarmonizer-style records
        keyed by slot title).
    schema : dict
        LinkML schema dict (e.g. from ``io.load_yaml``).
    target_class : str, optional
        Class to validate against. Defaults to the schema's ``dh_interface``
        main class found by ``schema.get_main_class``.
    strict : bool
        If True, stop validation after the first error per record.

    Returns
    -------
    ValidationReport
        ``report.results`` is a list of ``ValidationResult``. Use
        ``severity == Severity.ERROR`` to count failures.
    """
    if target_class is None:
        target_class, _ = get_main_class(schema)
        if target_class is None:
            raise ValueError("Schema has no dh_interface main class; specify target_class")

    validator = Validator(
        schema=schema,
        validation_plugins=[JsonschemaValidationPlugin()],
        strict=strict,
    )
    all_results: list[ValidationResult] = []
    for record in records:
        per_record = validator.validate(instance=record, target_class=target_class)
        all_results.extend(per_record.results)
    return ValidationReport(results=all_results)


def has_errors(report: ValidationReport) -> bool:
    """True if any result has severity ERROR or FATAL."""
    return any(r.severity in (Severity.ERROR, Severity.FATAL) for r in report.results)
