#!/usr/bin/env python3
"""Light behavioural tests for linkml_lib + dh_schema CLI.

Goal: smoke-test each operation. Uses small inline schema dicts where possible;
falls back to real fixtures for the conversion + DH-data tests.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from linkml_lib import convert_xml, convert_xsd, dh_data, io, pipeline, schema as schema_mod, transform
import dh_schema as dh_schema_cli


REPO = Path(__file__).parent.parent
ERC_XML = REPO / "assets" / "ena_schema" / "ERC000015.xml"
SRA_XSD = REPO / "assets" / "ena_schema" / "SRA.study.xsd"
DH_DATA = REPO / "assets" / "test-fixtures" / "ERC000015_example.json"


# ---------------------------------------------------------------------------
# Inline fixtures
# ---------------------------------------------------------------------------

def _schema(slots, enums=None, name="Demo"):
    """Build a minimal LinkML schema dict for tests."""
    slot_names = list(slots.keys())
    s = {
        "id": "https://example.org/Demo",
        "name": name,
        "title": name,
        "classes": {
            "dh_interface": {},
            name: {
                "is_a": "dh_interface",
                "slots": slot_names,
                "slot_usage": {n: {"rank": i + 1} for i, n in enumerate(slot_names)},
            },
        },
        "slots": slots,
    }
    if enums:
        s["enums"] = enums
    return s


@pytest.fixture
def schema_a():
    return _schema({
        "alias": {"title": "Alias", "range": "string", "required": True},
        "status": {"title": "Status", "range": "StatusMenu"},
    }, enums={"StatusMenu": {"permissible_values": {"NEW": {"text": "NEW"}, "OLD": {"text": "OLD"}}}})


@pytest.fixture
def schema_b():
    return _schema({
        "alias": {"title": "Alias (B)", "range": "string"},
        "extra": {"title": "Extra", "range": "string"},
    }, name="Other")


# ---------------------------------------------------------------------------
# Loaders / converters
# ---------------------------------------------------------------------------

def test_convert_xml_real_checklist():
    s = convert_xml.from_path(ERC_XML, "https://example.org")
    assert s is not None
    assert len(s["slots"]) > 50
    assert "project_name" in s["slots"]


def test_convert_xsd_real_xsd():
    s = convert_xsd.from_path(SRA_XSD, "https://example.org")
    assert s is not None
    assert "STUDY_TITLE" in s["slots"]


def test_load_any_dispatch_by_extension(tmp_path):
    yaml_file = tmp_path / "x.yaml"
    yaml_file.write_text("name: t\nslots: {}\nclasses: {}\n")
    assert io.load_any(yaml_file) is not None
    assert io.load_any(ERC_XML) is not None
    assert io.load_any(SRA_XSD) is not None
    assert io.load_any(tmp_path / "unknown.txt") is None


def test_write_yaml_lowercase_bool(tmp_path, schema_a):
    out = tmp_path / "o.yaml"
    io.write_yaml(schema_a, out)
    text = out.read_text()
    assert "required: true" in text
    assert "required: True" not in text


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------

def test_merge_priority(schema_a, schema_b):
    merged = transform.merge([schema_a, schema_b])
    # alias is in both — first (schema_a) wins → title "Alias", not "Alias (B)"
    assert merged["slots"]["alias"]["title"] == "Alias"
    # extra came from schema_b
    assert "extra" in merged["slots"]


def test_merge_renumbers_ranks(schema_a, schema_b):
    merged = transform.merge([schema_a, schema_b])
    ranks = [u["rank"] for u in merged["classes"][merged["name"]]["slot_usage"].values()]
    assert ranks == list(range(1, len(ranks) + 1))


# ---------------------------------------------------------------------------
# Filter
# ---------------------------------------------------------------------------

def test_filter_include(schema_a):
    out = transform.filter(schema_a, include=["alias"])
    assert list(out["slots"].keys()) == ["alias"]
    # StatusMenu was referenced only by "status" — pruned
    assert "enums" not in out


def test_filter_exclude(schema_a):
    out = transform.filter(schema_a, exclude=["status"])
    assert list(out["slots"].keys()) == ["alias"]


def test_filter_prunes_unused_enums(schema_a):
    out = transform.filter(schema_a, exclude=["status"])
    assert "enums" not in out


def test_filter_keeps_referenced_enums(schema_a):
    out = transform.filter(schema_a, include=["status"])
    assert "StatusMenu" in out["enums"]


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def test_pipeline_build_xml_plus_xsd():
    s = pipeline.build([str(SRA_XSD), str(ERC_XML)])
    assert len(s["slots"]) > 80
    assert s.get("enums")


def test_pipeline_build_raises_on_no_valid_inputs(tmp_path):
    bad = tmp_path / "bad.txt"
    bad.write_text("nothing")
    with pytest.raises(ValueError):
        pipeline.build([str(bad)])


# ---------------------------------------------------------------------------
# Schema introspection
# ---------------------------------------------------------------------------

def test_slot_meta_has_expected_columns(schema_a):
    rows = schema_mod.slot_meta(schema_a)
    assert len(rows) == 2
    assert set(rows[0].keys()) == set(schema_mod.SLOT_META_COLUMNS)


def test_summary_totals(schema_a):
    s = schema_mod.summary(schema_a)
    assert s["total_slots"] == 2
    assert s["required_slots"] == 1
    assert s["total_enums"] == 1


def test_diff_added_removed_changed(schema_a, schema_b):
    d = schema_mod.diff(schema_a, schema_b)
    assert "extra" in d["added"]
    assert "status" in d["removed"]
    # alias changed title between the two schemas
    assert any(c["name"] == "alias" for c in d["changed"])


# ---------------------------------------------------------------------------
# DataHarmonizer JSON data
# ---------------------------------------------------------------------------

def test_dh_filter_columns_real():
    with open(DH_DATA) as f:
        data = json.load(f)
    schema = io.load_xml(ERC_XML)
    out = dh_data.filter_columns(data, schema, "required = 1")
    rows = list(out["Container"].values())[0]
    if rows:
        # Only required-slot titles remain (or "alias")
        kept = set(rows[0].keys())
        assert kept  # at least some columns remain


def test_dh_remap_titles_to_names():
    schema = _schema({"alias": {"title": "Sample alias"}, "x": {"title": "X label"}})
    out = dh_data.remap_titles_to_names([{"Sample alias": "a1", "X label": "v"}], schema)
    assert out == [{"alias": "a1", "x": "v"}]


def test_dh_validate_required_missing():
    schema = _schema({"alias": {"name": "alias", "title": "Alias", "required": True}})
    report = dh_data.validate([{}], schema)
    assert dh_data.has_errors(report)
    assert any("alias" in r.message for r in report.results)


def test_dh_validate_passes():
    schema = _schema({"alias": {"name": "alias", "title": "Alias", "required": True}})
    report = dh_data.validate([{"alias": "x"}], schema)
    assert not dh_data.has_errors(report)


def test_dh_validate_enum_violation():
    schema = _schema(
        {"status": {"name": "status", "range": "StatusMenu"}},
        enums={"StatusMenu": {"permissible_values": {"NEW": {"text": "NEW"}}}},
    )
    report = dh_data.validate([{"status": "BOGUS"}], schema)
    assert dh_data.has_errors(report)
    assert any("status" in r.message or "BOGUS" in r.message for r in report.results)


# ---------------------------------------------------------------------------
# CLI smoke
# ---------------------------------------------------------------------------

def test_cli_build_smoke(tmp_path):
    runner = CliRunner()
    out = tmp_path / "out.yaml"
    result = runner.invoke(dh_schema_cli.app, ["build", str(ERC_XML), "-o", str(out)])
    assert result.exit_code == 0, result.output
    assert out.stat().st_size > 100


def test_cli_info_smoke(tmp_path):
    runner = CliRunner()
    yaml_file = tmp_path / "s.yaml"
    io.write_yaml(_schema({"alias": {"title": "A", "required": True}}), yaml_file)
    result = runner.invoke(dh_schema_cli.app, ["info", str(yaml_file)])
    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert parsed["total_slots"] == 1
