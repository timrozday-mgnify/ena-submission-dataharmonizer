#!/usr/bin/env python3
"""Unified CLI for DataHarmonizer LinkML schema operations.

Subcommands:
    convert       Convert an ENA checklist XML or XSD file → LinkML YAML
    merge         Merge multiple LinkML YAML schemas (priority = input order)
    filter        Filter slots by include/exclude lists, pruning unused enums
    build         Full pipeline: convert each input → merge → optional filter
    list-slots    Print a table of slot metadata for a schema
    info          Print summary counts (by source / slot_group / required / range)
    diff          Compare two schemas (added / removed / changed slots)
    filter-data   Filter columns of a DataHarmonizer JSON export by SQL WHERE
    remap-data    Remap record keys from slot titles → slot names
    validate-data Validate records against the schema (required, enums, types)

Examples:
    dh_schema.py build assets/ena_schema/*.xsd assets/ena_schema/ERC000015.xml -o out.yaml
    dh_schema.py filter in.yaml -o out.yaml --include keep.txt --exclude drop.txt
    dh_schema.py list-slots schemas/ERC000015.yaml
    dh_schema.py filter-data data.json schema.yaml --filter "source = 'ENA.sample'" -o filtered.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import typer

from linkml_lib import dh_data, io, pipeline, schema as schema_mod, transform
from linkml_lib.io import DEFAULT_BASE_URI
from linkml_lib.schema import SLOT_META_COLUMNS

app = typer.Typer(help="DataHarmonizer LinkML schema operations.", no_args_is_help=True)


# ---------------------------------------------------------------------------
# Schema commands
# ---------------------------------------------------------------------------

@app.command()
def convert(
    input_file: Path = typer.Argument(..., exists=True, help="ENA checklist XML or XSD file."),
    output: Path = typer.Option(..., "-o", "--output", help="Output LinkML YAML file."),
    base_uri: str = typer.Option(DEFAULT_BASE_URI, "--base-uri"),
) -> None:
    """Convert a single ENA checklist XML or XSD file to a LinkML YAML schema."""
    s = io.load_any(input_file, base_uri)
    if s is None:
        typer.echo(f"Error: could not convert {input_file} (unsupported type or empty content)", err=True)
        raise typer.Exit(1)
    io.write_yaml(s, output)
    _echo_counts(s, prefix="Written")
    typer.echo(f"  → {output}")


@app.command()
def merge(
    input_files: list[Path] = typer.Argument(..., help="LinkML YAML files; first = highest priority."),
    output: Path = typer.Option(..., "-o", "--output", help="Output merged YAML file."),
    name: str | None = typer.Option(None, "--name"),
    title: str | None = typer.Option(None, "--title"),
    description: str | None = typer.Option(None, "--description"),
    base_uri: str | None = typer.Option(None, "--base-uri"),
) -> None:
    """Merge multiple LinkML schemas into one (priority = input order)."""
    schemas = [io.load_yaml(p) for p in input_files]
    source_names = [p.stem for p in input_files]
    out = transform.merge(
        schemas, source_names=source_names,
        name=name, title=title, description=description, base_uri=base_uri,
    )
    io.write_yaml(out, output)
    _echo_counts(out, prefix=f"Merged {len(input_files)} schemas")
    typer.echo(f"  → {output}")


@app.command("filter")
def filter_cmd(
    input_file: Path = typer.Argument(..., exists=True, help="LinkML YAML schema."),
    output: Path = typer.Option(..., "-o", "--output", help="Output filtered YAML."),
    include: Path | None = typer.Option(None, "--include", help="File with slot names to include."),
    exclude: Path | None = typer.Option(None, "--exclude", help="File with slot names to exclude."),
) -> None:
    """Filter slots in a schema by include/exclude lists. Prunes unused enums."""
    if include is None and exclude is None:
        typer.echo("Error: at least one of --include or --exclude is required.", err=True)
        raise typer.Exit(1)
    s = io.load_yaml(input_file)
    inc = transform.load_field_list(include) if include else None
    exc = transform.load_field_list(exclude) if exclude else None
    out = transform.filter(s, include=inc, exclude=exc)
    io.write_yaml(out, output)
    _echo_counts(out, prefix="Filtered")
    typer.echo(f"  → {output}")


@app.command()
def build(
    input_files: list[Path] = typer.Argument(..., help="Inputs: .xsd, .xml, .yaml/.yml. Order = priority."),
    output: Path = typer.Option(..., "-o", "--output", help="Output merged YAML."),
    include: Path | None = typer.Option(None, "--include"),
    exclude: Path | None = typer.Option(None, "--exclude"),
    name: str | None = typer.Option(None, "--name"),
    title: str | None = typer.Option(None, "--title"),
    description: str | None = typer.Option(None, "--description"),
    base_uri: str = typer.Option(DEFAULT_BASE_URI, "--base-uri"),
) -> None:
    """Convert each input → merge → optionally filter → write YAML."""
    inc = transform.load_field_list(include) if include else None
    exc = transform.load_field_list(exclude) if exclude else None
    try:
        out = pipeline.build(
            [str(p) for p in input_files],
            base_uri=base_uri, name=name, title=title, description=description,
            include=inc, exclude=exc,
        )
    except ValueError as exc_msg:
        typer.echo(f"Error: {exc_msg}", err=True)
        raise typer.Exit(1)
    io.write_yaml(out, output)
    _echo_counts(out, prefix=f"Built from {len(input_files)} input(s)")
    typer.echo(f"  → {output}")


# ---------------------------------------------------------------------------
# Introspection commands
# ---------------------------------------------------------------------------

@app.command("list-slots")
def list_slots_cmd(
    input_file: Path = typer.Argument(..., exists=True, help="LinkML YAML schema."),
) -> None:
    """Print the slot metadata table for a schema (name, title, source, ...)."""
    s = io.load_yaml(input_file)
    rows = schema_mod.slot_meta(s)
    _print_slot_table(rows)


@app.command()
def info(
    input_file: Path = typer.Argument(..., exists=True, help="LinkML YAML schema."),
) -> None:
    """Print summary counts (totals + breakdown by source / slot_group / range)."""
    s = io.load_yaml(input_file)
    summary = schema_mod.summary(s)
    typer.echo(json.dumps(summary, indent=2, ensure_ascii=False))


@app.command()
def diff(
    a: Path = typer.Argument(..., exists=True, help="First LinkML YAML schema."),
    b: Path = typer.Argument(..., exists=True, help="Second LinkML YAML schema."),
) -> None:
    """Show slots added / removed / changed between two schemas."""
    result = schema_mod.diff(io.load_yaml(a), io.load_yaml(b))
    typer.echo(json.dumps(result, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# DataHarmonizer JSON data commands
# ---------------------------------------------------------------------------

@app.command("filter-data")
def filter_data_cmd(
    input_file: Path = typer.Argument(..., exists=True, help="DataHarmonizer JSON export."),
    schema_file: Path = typer.Argument(..., exists=True, help="LinkML YAML schema."),
    filter_expr: str = typer.Option(..., "--filter", "-f",
                                    help="SQL WHERE clause on slots table "
                                    "(columns: name, title, source, required 0/1, slot_group, rank, range)."),
    output: Path | None = typer.Option(None, "-o", "--output",
                                       help="Output file (default: stdout)."),
) -> None:
    """Filter DH JSON columns by a SQL WHERE clause on slot metadata."""
    data = json.loads(input_file.read_text())
    s = io.load_yaml(schema_file)
    try:
        out = dh_data.filter_columns(data, s, filter_expr)
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)
    text = json.dumps(out, indent=2, ensure_ascii=False)
    if output:
        output.write_text(text)
    else:
        sys.stdout.write(text + "\n")


@app.command("remap-data")
def remap_data_cmd(
    input_file: Path = typer.Argument(..., exists=True, help="DataHarmonizer JSON export."),
    schema_file: Path = typer.Argument(..., exists=True, help="LinkML YAML schema."),
    output: Path | None = typer.Option(None, "-o", "--output"),
) -> None:
    """Remap record keys from slot titles (DH exports) to slot names."""
    data = json.loads(input_file.read_text())
    s = io.load_yaml(schema_file)
    records = _records_from_dh(data)
    out_records = dh_data.remap_titles_to_names(records, s)
    text = json.dumps(out_records, indent=2, ensure_ascii=False)
    if output:
        output.write_text(text)
    else:
        sys.stdout.write(text + "\n")


@app.command("validate-data")
def validate_data_cmd(
    input_file: Path = typer.Argument(..., exists=True, help="DataHarmonizer JSON or list of records."),
    schema_file: Path = typer.Argument(..., exists=True, help="LinkML YAML schema."),
) -> None:
    """Validate records against the schema (required fields, enums, types).

    Exits non-zero if any record fails validation. Prints all messages.
    """
    data = json.loads(input_file.read_text())
    s = io.load_yaml(schema_file)
    records = _records_from_dh(data)
    is_valid, messages = dh_data.validate(records, s)
    for msg in messages:
        typer.echo(msg)
    raise typer.Exit(0 if is_valid else 1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _records_from_dh(data):
    """Extract the record list from a DH JSON export, list, or single-record dict."""
    if isinstance(data, list):
        return data
    container = data.get("Container") if isinstance(data, dict) else None
    if isinstance(container, dict):
        return next(iter(container.values()))
    return [data] if isinstance(data, dict) else []


def _echo_counts(s, *, prefix: str) -> None:
    n_slots = len(s.get("slots") or {})
    n_required = sum(1 for v in (s.get("slots") or {}).values() if v.get("required"))
    n_enums = len(s.get("enums") or {})
    typer.echo(f"{prefix}: {n_slots} slots ({n_required} required), {n_enums} enums")


def _print_slot_table(rows):
    headers = list(SLOT_META_COLUMNS)
    table = [
        [r["name"], r["title"], r["source"], str(int(r["required"])),
         r["slot_group"], "" if r["rank"] == 9999 else str(r["rank"]), r["range"]]
        for r in rows
    ]
    widths = [max(len(h), max((len(cell) for cell in col), default=0))
              for h, col in zip(headers, zip(*table) if table else [[]] * len(headers))]
    typer.echo("  ".join(h.ljust(w) for h, w in zip(headers, widths)))
    typer.echo("  ".join("-" * w for w in widths))
    for row in table:
        typer.echo("  ".join(cell.ljust(w) for cell, w in zip(row, widths)))


if __name__ == "__main__":
    app()
