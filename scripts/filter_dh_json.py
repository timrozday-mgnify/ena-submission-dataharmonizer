#!/usr/bin/env python3
"""Filter DataHarmonizer JSON exports by slot metadata from their LinkML schema.

Uses an in-memory SQLite table (stdlib — no extra dependencies) to evaluate a
SQL WHERE expression against per-slot metadata fields.

Available columns in the ``slots`` table:

    name       TEXT     — slot identifier (e.g. ``alias``, ``TAXON_ID``)
    title      TEXT     — human-readable label (e.g. ``Sample alias``)
    source     TEXT     — ``annotations.source`` value (e.g. ``ENA.sample``, ``ERC000025``)
    required   INTEGER  — 1 if required, 0 otherwise
    slot_group TEXT     — UI section header (e.g. ``Identifiers``, ``DNA extraction``)
    rank       INTEGER  — column ordering from slot_usage
    range      TEXT     — data type (e.g. ``string``, ``integer``, or an enum name)

Usage::

    # Discover available slot metadata for a schema
    python scripts/filter_dh_json.py data.json schema.yaml --list-slots

    # Keep only slots from a specific source
    python scripts/filter_dh_json.py data.json schema.yaml \\
        --filter "source = 'ENA.sample'"

    # OR logic: source or required
    python scripts/filter_dh_json.py data.json schema.yaml \\
        --filter "source = 'ENA.sample' OR required = 1"

    # LIKE patterns and IN lists
    python scripts/filter_dh_json.py data.json schema.yaml \\
        --filter "source LIKE 'ENA.%' OR name IN ('alias', 'TAXON_ID')"

    # Write to a file instead of stdout
    python scripts/filter_dh_json.py data.json schema.yaml \\
        --filter "source LIKE 'ERC%'" -o filtered.json
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import typer

import ena_common as common

app = typer.Typer(help="Filter DataHarmonizer JSON columns by LinkML slot metadata (SQL WHERE).")


def _get_main_class(schema: dict[str, Any]) -> tuple[str | None, dict | None]:
    for name, cls in schema.get("classes", {}).items():
        if isinstance(cls, dict) and cls.get("is_a") == "dh_interface":
            return name, cls
    return None, None


def _build_slots_meta(schema: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract filterable slot metadata from a LinkML schema."""
    _, main_cls = _get_main_class(schema)
    if main_cls is None:
        raise ValueError("Schema has no main class (no class with is_a: dh_interface)")
    slot_usage = main_cls.get("slot_usage") or {}
    result = []
    for slot_name in main_cls.get("slots") or []:
        slot = (schema.get("slots") or {}).get(slot_name) or {}
        usage = slot_usage.get(slot_name) or {}
        result.append({
            "name": slot_name,
            "title": slot.get("title", slot_name),
            "source": (slot.get("annotations") or {}).get("source", ""),
            "required": bool(slot.get("required", False)),
            "slot_group": usage.get("slot_group", ""),
            "rank": usage.get("rank", 9999),
            "range": slot.get("range", "string"),
        })
    return result


def _filter_slots(slots_meta: list[dict[str, Any]], where: str) -> set[str]:
    """Return slot names matching the SQL WHERE clause."""
    con = sqlite3.connect(":memory:")
    con.execute(
        "CREATE TABLE slots "
        "(name TEXT, title TEXT, source TEXT, required INTEGER, "
        "slot_group TEXT, rank INTEGER, range TEXT)"
    )
    con.executemany("INSERT INTO slots VALUES (?,?,?,?,?,?,?)", [
        (s["name"], s["title"], s["source"], int(s["required"]),
         s["slot_group"], s["rank"], s["range"])
        for s in slots_meta
    ])
    try:
        rows = con.execute(f"SELECT name FROM slots WHERE {where}").fetchall()
    except sqlite3.OperationalError as exc:
        raise ValueError(f"Invalid SQL WHERE clause: {exc}") from exc
    return {row[0] for row in rows}


def _print_slots_table(slots_meta: list[dict[str, Any]]) -> None:
    """Print slot metadata as a formatted table."""
    headers = ["name", "title", "source", "required", "slot_group", "rank", "range"]
    rows = [
        [
            s["name"],
            s["title"],
            s["source"],
            str(int(s["required"])),
            s["slot_group"],
            str(s["rank"]) if s["rank"] != 9999 else "",
            s["range"],
        ]
        for s in slots_meta
    ]
    widths = [
        max(len(h), max((len(r[i]) for r in rows), default=0))
        for i, h in enumerate(headers)
    ]
    sep = "  ".join("-" * w for w in widths)
    typer.echo("  ".join(h.ljust(w) for h, w in zip(headers, widths)))
    typer.echo(sep)
    for row in rows:
        typer.echo("  ".join(cell.ljust(w) for cell, w in zip(row, widths)))


def _load_dh_json(path: Path) -> tuple[dict[str, Any], str, list[dict[str, Any]]]:
    """Load a DataHarmonizer JSON export. Returns (full_data, container_key, records)."""
    data = json.loads(path.read_text())
    container = data.get("Container", data)
    if not isinstance(container, dict):
        raise ValueError("Expected JSON with a 'Container' object at the top level")
    container_key = next(iter(container))
    records = container[container_key]
    if not isinstance(records, list):
        raise ValueError(f"Container.{container_key!r} is not a list of records")
    return data, container_key, records


@app.command()
def main(
    input_file: Path = typer.Argument(..., exists=True, help="DataHarmonizer JSON export"),
    schema: Path = typer.Argument(..., exists=True, help="LinkML YAML schema"),
    filter_expr: str | None = typer.Option(
        None, "--filter", "-f",
        help=(
            "SQL WHERE clause against the slots table. "
            "Columns: name, title, source, required (0/1), slot_group, rank, range. "
            "Example: \"source = 'ENA.sample' OR required = 1\""
        ),
    ),
    list_slots: bool = typer.Option(
        False, "--list-slots",
        help="Print the slot metadata table for this schema and exit (useful for building filter expressions)",
    ),
    output: Path | None = typer.Option(None, "-o", "--output", help="Output file path (default: stdout)"),
) -> None:
    """Filter DataHarmonizer JSON columns by LinkML slot metadata.

    Loads the slot metadata from the LinkML schema into an in-memory SQLite table
    and evaluates the given SQL WHERE clause to select which columns to keep.
    The output JSON preserves the original DataHarmonizer Container structure.

    ``required`` is stored as INTEGER (1/0). SQLite 3.23+ also accepts TRUE/FALSE.
    """
    schema_dict = common.load_linkml_schema(schema)

    try:
        slots_meta = _build_slots_meta(schema_dict)
    except ValueError as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(1)

    if list_slots:
        _print_slots_table(slots_meta)
        raise typer.Exit()

    if not filter_expr:
        typer.echo(
            "ERROR: --filter is required. Use --list-slots to inspect available slot metadata.",
            err=True,
        )
        raise typer.Exit(1)

    try:
        selected_names = _filter_slots(slots_meta, filter_expr)
    except ValueError as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(1)

    if not selected_names:
        typer.echo("WARNING: filter matched no slots — output will have empty rows", err=True)

    title_to_name = common.build_title_to_slot_map(schema_dict)
    name_to_title = {v: k for k, v in title_to_name.items()}
    keep_titles = {name_to_title.get(n, n) for n in selected_names}

    try:
        data, container_key, records = _load_dh_json(input_file)
    except (ValueError, json.JSONDecodeError) as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(1)

    filtered_records = [
        {k: v for k, v in row.items() if k in keep_titles}
        for row in records
    ]

    out = {**data, "Container": {container_key: filtered_records}}
    out_text = json.dumps(out, indent=2, ensure_ascii=False)

    if output:
        output.write_text(out_text)
    else:
        sys.stdout.write(out_text + "\n")


if __name__ == "__main__":
    app()
