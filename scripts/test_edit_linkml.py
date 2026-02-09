#!/usr/bin/env python3
"""Tests for edit_linkml.py – Elasticsearch search integration and helpers.

Requires:
  - A running Elasticsearch instance at http://localhost:9200
  - The elasticsearch Python package (v8.x)
  - pytest and pytest-asyncio

Usage:
    pytest scripts/test_edit_linkml.py -v
    pytest scripts/test_edit_linkml.py -v -k "not es_integration"  # skip ES tests
"""

import os
import sys
import tempfile
import uuid

import pytest
import yaml

# Ensure the scripts directory is importable
sys.path.insert(0, os.path.dirname(__file__))

from edit_linkml import (
    LinkMLEditor,
    extract_enums,
    extract_fields,
    get_main_class,
    load_schema,
    rebuild_schema,
    save_schema,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "..", "schemas", "ERC000015.yaml")
EMBRACE_PATH = os.path.join(os.path.dirname(__file__), "..", "schemas", "embrace.yaml")


@pytest.fixture
def sample_schema():
    """Load the ERC000015 schema for testing."""
    with open(SCHEMA_PATH, "r") as f:
        return yaml.safe_load(f)


@pytest.fixture
def sample_fields(sample_schema):
    return extract_fields(sample_schema)


@pytest.fixture
def minimal_schema():
    """A minimal LinkML schema for unit tests (no file dependency)."""
    return {
        "id": "https://example.com/test",
        "name": "test",
        "title": "Test Schema",
        "description": "For testing",
        "version": "1.0.0",
        "imports": ["linkml:types"],
        "prefixes": {"linkml": {"prefix_reference": "https://w3id.org/linkml/"}},
        "default_range": "string",
        "classes": {
            "dh_interface": {
                "name": "dh_interface",
                "description": "A DataHarmonizer interface",
            },
            "test": {
                "name": "test",
                "title": "Test Schema",
                "is_a": "dh_interface",
                "slots": ["sample_name", "sample_collection_date", "host_organism"],
                "slot_usage": {
                    "sample_name": {"rank": 1, "slot_group": "Sample info"},
                    "sample_collection_date": {"rank": 2, "slot_group": "Sample info"},
                    "host_organism": {"rank": 3, "slot_group": "Host"},
                },
            },
        },
        "slots": {
            "sample_name": {
                "name": "sample_name",
                "title": "Sample Name",
                "description": "The name of the sample",
                "required": True,
                "range": "string",
            },
            "sample_collection_date": {
                "name": "sample_collection_date",
                "title": "Collection Date",
                "description": "Date the sample was collected from the environment",
                "range": "string",
                "pattern": r"^\d{4}-\d{2}-\d{2}$",
            },
            "host_organism": {
                "name": "host_organism",
                "title": "Host Organism",
                "description": "The host organism of the sample",
                "range": "HostMenu",
            },
        },
        "enums": {
            "HostMenu": {
                "name": "HostMenu",
                "permissible_values": {
                    "Human": {"text": "Human", "description": "Homo sapiens"},
                    "Mouse": {"text": "Mouse", "description": "Mus musculus"},
                },
            },
        },
    }


@pytest.fixture
def minimal_fields(minimal_schema):
    return extract_fields(minimal_schema)


def _es_available() -> bool:
    """Check if Elasticsearch is reachable."""
    try:
        from elasticsearch import Elasticsearch
        es = Elasticsearch("http://localhost:9200")
        es.info()
        return True
    except Exception:
        return False


es_integration = pytest.mark.skipif(
    not _es_available(),
    reason="Elasticsearch not available at localhost:9200",
)


# ---------------------------------------------------------------------------
# Data extraction tests
# ---------------------------------------------------------------------------

class TestExtractFields:
    def test_extracts_fields(self, sample_schema):
        fields = extract_fields(sample_schema)
        assert len(fields) > 0

    def test_field_keys(self, sample_schema):
        fields = extract_fields(sample_schema)
        expected_keys = {"name", "title", "description", "range", "pattern",
                         "required", "comments", "slot_group", "rank", "source"}
        for f in fields:
            assert expected_keys.issubset(f.keys())

    def test_minimal_schema(self, minimal_schema):
        fields = extract_fields(minimal_schema)
        assert len(fields) == 3
        names = [f["name"] for f in fields]
        assert names == ["sample_name", "sample_collection_date", "host_organism"]

    def test_required_field(self, minimal_schema):
        fields = extract_fields(minimal_schema)
        sample_name = next(f for f in fields if f["name"] == "sample_name")
        assert sample_name["required"] is True

    def test_slot_group(self, minimal_schema):
        fields = extract_fields(minimal_schema)
        sample_name = next(f for f in fields if f["name"] == "sample_name")
        assert sample_name["slot_group"] == "Sample info"


class TestExtractEnums:
    def test_extracts_enums(self, sample_schema):
        enums = extract_enums(sample_schema)
        assert len(enums) > 0

    def test_enum_keys(self, sample_schema):
        enums = extract_enums(sample_schema)
        for e in enums:
            assert "enum_name" in e
            assert "value" in e

    def test_minimal_enums(self, minimal_schema):
        enums = extract_enums(minimal_schema)
        assert len(enums) == 2
        values = {e["value"] for e in enums}
        assert values == {"Human", "Mouse"}


class TestGetMainClass:
    def test_finds_main_class(self, minimal_schema):
        name, cls = get_main_class(minimal_schema)
        assert name == "test"
        assert cls["is_a"] == "dh_interface"

    def test_empty_schema(self):
        name, cls = get_main_class({})
        assert name == ""
        assert cls == {}


class TestRebuildSchema:
    def test_roundtrip(self, minimal_schema):
        fields = extract_fields(minimal_schema)
        enums = extract_enums(minimal_schema)
        rebuilt = rebuild_schema(minimal_schema, fields, enums)
        # Main class should exist
        name, cls = get_main_class(rebuilt)
        assert name == "test"
        assert len(cls["slots"]) == 3


# ---------------------------------------------------------------------------
# Save / load round-trip tests (rank persistence & inserted fields)
# ---------------------------------------------------------------------------

class TestSaveRankOrder:
    """Verify that modified field ordering (rank) survives save → load."""

    def test_swapped_ranks_persist(self, minimal_schema):
        """Swap two fields' ranks, save, reload, and verify the new order."""
        fields = extract_fields(minimal_schema)
        enums = extract_enums(minimal_schema)

        # Original order: sample_name(1), sample_collection_date(2), host_organism(3)
        assert fields[0]["name"] == "sample_name"
        assert fields[1]["name"] == "sample_collection_date"
        assert fields[2]["name"] == "host_organism"

        # Swap ranks of first two fields (simulating action_rank_down on sample_name)
        fields[0]["rank"], fields[1]["rank"] = fields[1]["rank"], fields[0]["rank"]

        # Sort by rank before rebuilding (as the app now does)
        ordered = sorted(fields, key=lambda f: f.get("rank", 0))
        rebuilt = rebuild_schema(minimal_schema, ordered, enums)

        with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            save_schema(rebuilt, tmp_path)
            loaded = load_schema(tmp_path)
            loaded_fields = extract_fields(loaded)

            names = [f["name"] for f in loaded_fields]
            assert names == ["sample_collection_date", "sample_name", "host_organism"]
            assert loaded_fields[0]["rank"] == 1
            assert loaded_fields[1]["rank"] == 2
            assert loaded_fields[2]["rank"] == 3
        finally:
            os.unlink(tmp_path)

    def test_reverse_order_persists(self, minimal_schema):
        """Reverse the entire field order, save, reload, and verify."""
        fields = extract_fields(minimal_schema)
        enums = extract_enums(minimal_schema)

        # Reverse ranks: host_organism=1, sample_collection_date=2, sample_name=3
        fields[0]["rank"] = 3
        fields[1]["rank"] = 2
        fields[2]["rank"] = 1

        ordered = sorted(fields, key=lambda f: f.get("rank", 0))
        rebuilt = rebuild_schema(minimal_schema, ordered, enums)

        with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            save_schema(rebuilt, tmp_path)
            loaded = load_schema(tmp_path)
            loaded_fields = extract_fields(loaded)

            names = [f["name"] for f in loaded_fields]
            assert names == ["host_organism", "sample_collection_date", "sample_name"]
            # Ranks should be renumbered sequentially
            assert [f["rank"] for f in loaded_fields] == [1, 2, 3]
        finally:
            os.unlink(tmp_path)

    def test_slot_groups_preserved_after_reorder(self, minimal_schema):
        """Slot group assignments survive a rank reorder."""
        fields = extract_fields(minimal_schema)
        enums = extract_enums(minimal_schema)

        # Move host_organism (rank 3, group "Host") to rank 1
        fields[2]["rank"] = 0  # before everything
        ordered = sorted(fields, key=lambda f: f.get("rank", 0))
        rebuilt = rebuild_schema(minimal_schema, ordered, enums)

        with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            save_schema(rebuilt, tmp_path)
            loaded = load_schema(tmp_path)
            loaded_fields = extract_fields(loaded)

            first = loaded_fields[0]
            assert first["name"] == "host_organism"
            assert first["slot_group"] == "Host"
        finally:
            os.unlink(tmp_path)


class TestSaveInsertedFields:
    """Verify that newly inserted fields survive save → load."""

    def test_inserted_field_persists(self, minimal_schema):
        """Insert a new field, save, reload, and verify it exists."""
        fields = extract_fields(minimal_schema)
        enums = extract_enums(minimal_schema)

        new_field = {
            "name": "new_measurement",
            "title": "New Measurement",
            "description": "A freshly added field",
            "range": "string",
            "slot_group": "Sample info",
            "required": False,
            "pattern": "",
            "comments": "",
            "rank": 4,
            "source": "",
        }
        fields.append(new_field)

        ordered = sorted(fields, key=lambda f: f.get("rank", 0))
        rebuilt = rebuild_schema(minimal_schema, ordered, enums)

        with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            save_schema(rebuilt, tmp_path)
            loaded = load_schema(tmp_path)
            loaded_fields = extract_fields(loaded)

            names = [f["name"] for f in loaded_fields]
            assert "new_measurement" in names
            assert len(loaded_fields) == 4

            new_f = next(f for f in loaded_fields if f["name"] == "new_measurement")
            assert new_f["title"] == "New Measurement"
            assert new_f["description"] == "A freshly added field"
            assert new_f["slot_group"] == "Sample info"
            assert new_f["rank"] == 4
        finally:
            os.unlink(tmp_path)

    def test_inserted_field_with_attributes(self, minimal_schema):
        """Insert a field with non-default attributes (required, pattern, range)."""
        fields = extract_fields(minimal_schema)
        enums = extract_enums(minimal_schema)

        new_field = {
            "name": "ph_level",
            "title": "pH Level",
            "description": "Measured pH value",
            "range": "float",
            "slot_group": "Measurements",
            "required": True,
            "pattern": r"^\d+\.\d+$",
            "comments": "Must be numeric",
            "rank": 4,
            "source": "custom",
        }
        fields.append(new_field)

        ordered = sorted(fields, key=lambda f: f.get("rank", 0))
        rebuilt = rebuild_schema(minimal_schema, ordered, enums)

        with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            save_schema(rebuilt, tmp_path)
            loaded = load_schema(tmp_path)
            loaded_fields = extract_fields(loaded)

            ph = next(f for f in loaded_fields if f["name"] == "ph_level")
            assert ph["required"] is True
            assert ph["pattern"] == r"^\d+\.\d+$"
            assert ph["range"] == "float"
            assert ph["slot_group"] == "Measurements"
            assert ph["source"] == "custom"
            assert "numeric" in ph["comments"]
        finally:
            os.unlink(tmp_path)

    def test_multiple_inserted_fields(self, minimal_schema):
        """Insert several fields and verify all persist after save → load."""
        fields = extract_fields(minimal_schema)
        enums = extract_enums(minimal_schema)

        for i in range(3):
            fields.append({
                "name": f"extra_field_{i}",
                "title": f"Extra Field {i}",
                "description": f"Extra description {i}",
                "range": "string",
                "slot_group": "",
                "required": False,
                "pattern": "",
                "comments": "",
                "rank": 4 + i,
                "source": "",
            })

        ordered = sorted(fields, key=lambda f: f.get("rank", 0))
        rebuilt = rebuild_schema(minimal_schema, ordered, enums)

        with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            save_schema(rebuilt, tmp_path)
            loaded = load_schema(tmp_path)
            loaded_fields = extract_fields(loaded)

            assert len(loaded_fields) == 6
            names = [f["name"] for f in loaded_fields]
            for i in range(3):
                assert f"extra_field_{i}" in names
        finally:
            os.unlink(tmp_path)

    def test_insert_then_reorder(self, minimal_schema):
        """Insert a field, reorder it ahead of existing ones, save, verify."""
        fields = extract_fields(minimal_schema)
        enums = extract_enums(minimal_schema)

        new_field = {
            "name": "priority_field",
            "title": "Priority Field",
            "description": "Should appear first after reorder",
            "range": "string",
            "slot_group": "Sample info",
            "required": False,
            "pattern": "",
            "comments": "",
            "rank": 4,
            "source": "",
        }
        fields.append(new_field)

        # Now reorder: give new field rank 0 so it comes first
        for f in fields:
            if f["name"] == "priority_field":
                f["rank"] = 0

        ordered = sorted(fields, key=lambda f: f.get("rank", 0))
        rebuilt = rebuild_schema(minimal_schema, ordered, enums)

        with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            save_schema(rebuilt, tmp_path)
            loaded = load_schema(tmp_path)
            loaded_fields = extract_fields(loaded)

            assert loaded_fields[0]["name"] == "priority_field"
            assert loaded_fields[0]["rank"] == 1
            assert len(loaded_fields) == 4
        finally:
            os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# Highlight helper tests (no ES needed)
# ---------------------------------------------------------------------------

class TestExtractHighlightTerms:
    def test_single_term(self):
        fragments = ["The <em>sample</em> was collected"]
        terms = LinkMLEditor._extract_highlight_terms(fragments)
        assert terms == {"sample"}

    def test_multiple_terms(self):
        fragments = ["<em>sample</em> <em>collection</em> date"]
        terms = LinkMLEditor._extract_highlight_terms(fragments)
        assert terms == {"sample", "collection"}

    def test_no_highlights(self):
        fragments = ["no highlights here"]
        terms = LinkMLEditor._extract_highlight_terms(fragments)
        assert terms == set()

    def test_empty(self):
        assert LinkMLEditor._extract_highlight_terms([]) == set()

    def test_multiple_fragments(self):
        fragments = ["<em>foo</em> bar", "baz <em>qux</em>"]
        terms = LinkMLEditor._extract_highlight_terms(fragments)
        assert terms == {"foo", "qux"}


class TestHighlightCell:
    """Test the _highlight_cell static-like method. Needs an instance."""

    @pytest.fixture
    def editor(self):
        return LinkMLEditor.__new__(LinkMLEditor)

    def test_no_terms(self, editor):
        result = editor._highlight_cell("hello world", set())
        assert str(result) == "hello world"

    def test_single_match(self, editor):
        result = editor._highlight_cell("hello world", {"world"})
        assert str(result) == "hello world"
        # The "world" part should be highlighted (bold yellow)
        assert len(result._spans) > 0

    def test_case_insensitive(self, editor):
        result = editor._highlight_cell("Hello World", {"hello"})
        assert str(result) == "Hello World"
        assert len(result._spans) > 0

    def test_multiple_matches(self, editor):
        result = editor._highlight_cell("sample collection sample", {"sample"})
        assert str(result) == "sample collection sample"
        # Should have highlights for both occurrences
        assert len(result._spans) >= 2

    def test_overlapping_terms(self, editor):
        result = editor._highlight_cell("foobar", {"foo", "foobar"})
        assert str(result) == "foobar"

    def test_empty_text(self, editor):
        result = editor._highlight_cell("", {"sample"})
        assert str(result) == ""

    def test_with_base_style(self, editor):
        result = editor._highlight_cell("hello world", {"world"}, base_style="on dark_blue")
        assert str(result) == "hello world"


# ---------------------------------------------------------------------------
# Elasticsearch integration tests
# ---------------------------------------------------------------------------

class TestElasticsearchIntegration:
    """Tests that require a running Elasticsearch instance."""

    @pytest.fixture
    def es_client(self):
        from elasticsearch import Elasticsearch
        return Elasticsearch("http://localhost:9200")

    @pytest.fixture
    def es_index(self, es_client):
        """Create a temporary ES index and clean it up after the test."""
        idx = f"test_linkml_{uuid.uuid4().hex[:8]}"
        yield idx
        try:
            es_client.options(ignore_status=[404]).indices.delete(index=idx)
        except Exception:
            pass

    @es_integration
    def test_connection(self, es_client):
        info = es_client.info()
        assert "version" in info

    @es_integration
    def test_index_and_search(self, es_client, es_index, minimal_fields):
        """Test basic indexing and search of fields."""
        # Create index with the same mappings as the app
        es_client.indices.create(
            index=es_index,
            settings={
                "analysis": {
                    "analyzer": {
                        "underscore_analyzer": {
                            "type": "pattern",
                            "pattern": r"[_\s\-\.]+",
                            "lowercase": True,
                        }
                    }
                }
            },
            mappings={
                "properties": {
                    "name": {"type": "text", "analyzer": "underscore_analyzer",
                             "fields": {"keyword": {"type": "keyword"}}},
                    "title": {"type": "text"},
                    "description": {"type": "text"},
                    "range": {"type": "text", "analyzer": "underscore_analyzer",
                              "fields": {"keyword": {"type": "keyword"}}},
                    "pattern": {"type": "text"},
                    "required": {"type": "text"},
                    "comments": {"type": "text"},
                    "slot_group": {"type": "text", "analyzer": "underscore_analyzer",
                                   "fields": {"keyword": {"type": "keyword"}}},
                    "source": {"type": "text", "analyzer": "underscore_analyzer",
                               "fields": {"keyword": {"type": "keyword"}}},
                    "rank": {"type": "text"},
                }
            },
        )

        # Index fields
        for field in minimal_fields:
            doc = {
                "name": field.get("name", ""),
                "title": field.get("title", ""),
                "description": field.get("description", ""),
                "range": field.get("range", ""),
                "pattern": field.get("pattern", ""),
                "required": "Yes" if field.get("required") else "No",
                "comments": field.get("comments", ""),
                "slot_group": field.get("slot_group", ""),
                "source": field.get("source", ""),
                "rank": str(field.get("rank", 0)),
            }
            es_client.index(index=es_index, id=field["name"], document=doc)
        es_client.indices.refresh(index=es_index)

        # Search all
        resp = es_client.search(index=es_index, query={"match_all": {}})
        assert resp["hits"]["total"]["value"] == 3

        # Search by term
        resp = es_client.search(
            index=es_index,
            query={"query_string": {"query": "sample", "default_operator": "AND", "lenient": True}},
        )
        assert resp["hits"]["total"]["value"] >= 2  # sample_name and sample_collection_date

    @es_integration
    def test_underscore_analyzer_splits_names(self, es_client, es_index, minimal_fields):
        """Test that the underscore analyzer allows searching name:sample."""
        es_client.indices.create(
            index=es_index,
            settings={
                "analysis": {
                    "analyzer": {
                        "underscore_analyzer": {
                            "type": "pattern",
                            "pattern": r"[_\s\-\.]+",
                            "lowercase": True,
                        }
                    }
                }
            },
            mappings={
                "properties": {
                    "name": {"type": "text", "analyzer": "underscore_analyzer",
                             "fields": {"keyword": {"type": "keyword"}}},
                }
            },
        )
        es_client.index(index=es_index, id="1", document={"name": "sample_collection_device"})
        es_client.index(index=es_index, id="2", document={"name": "host_organism"})
        es_client.indices.refresh(index=es_index)

        # name:sample should match because underscore_analyzer splits on _
        resp = es_client.search(
            index=es_index,
            query={"query_string": {"query": "name:sample", "default_operator": "AND"}},
        )
        assert resp["hits"]["total"]["value"] == 1
        assert resp["hits"]["hits"][0]["_id"] == "1"

    @es_integration
    def test_search_with_highlights(self, es_client, es_index, minimal_fields):
        """Test that search highlights are returned correctly."""
        es_client.indices.create(
            index=es_index,
            settings={
                "analysis": {
                    "analyzer": {
                        "underscore_analyzer": {
                            "type": "pattern",
                            "pattern": r"[_\s\-\.]+",
                            "lowercase": True,
                        }
                    }
                }
            },
            mappings={
                "properties": {
                    "name": {"type": "text", "analyzer": "underscore_analyzer",
                             "fields": {"keyword": {"type": "keyword"}}},
                    "title": {"type": "text"},
                    "description": {"type": "text"},
                }
            },
        )
        es_client.index(
            index=es_index, id="sample_name",
            document={"name": "sample_name", "title": "Sample Name", "description": "The name of the sample"},
        )
        es_client.indices.refresh(index=es_index)

        resp = es_client.search(
            index=es_index,
            query={"query_string": {"query": "sample", "default_operator": "AND", "lenient": True}},
            highlight={
                "fields": {"*": {}},
                "pre_tags": ["<em>"],
                "post_tags": ["</em>"],
                "number_of_fragments": 0,
            },
            size=10000,
        )
        hits = resp["hits"]["hits"]
        assert len(hits) == 1
        hl = hits[0].get("highlight", {})
        assert len(hl) > 0
        # Check that highlights contain <em> tags
        for field_name, fragments in hl.items():
            for frag in fragments:
                assert "<em>" in frag

    @es_integration
    def test_wildcard_search(self, es_client, es_index):
        """Test wildcard queries."""
        es_client.indices.create(
            index=es_index,
            settings={
                "analysis": {
                    "analyzer": {
                        "underscore_analyzer": {
                            "type": "pattern",
                            "pattern": r"[_\s\-\.]+",
                            "lowercase": True,
                        }
                    }
                }
            },
            mappings={"properties": {
                "name": {"type": "text", "analyzer": "underscore_analyzer"},
            }},
        )
        es_client.index(index=es_index, id="1", document={"name": "sample_name"})
        es_client.index(index=es_index, id="2", document={"name": "host_organism"})
        es_client.indices.refresh(index=es_index)

        resp = es_client.search(
            index=es_index,
            query={"query_string": {"query": "samp*", "default_operator": "AND",
                                     "analyze_wildcard": True}},
        )
        assert resp["hits"]["total"]["value"] == 1

    @es_integration
    def test_required_field_search(self, es_client, es_index):
        """Test searching for required:Yes."""
        es_client.indices.create(
            index=es_index,
            mappings={"properties": {"required": {"type": "text"}, "name": {"type": "text"}}},
        )
        es_client.index(index=es_index, id="1", document={"name": "field_a", "required": "Yes"})
        es_client.index(index=es_index, id="2", document={"name": "field_b", "required": "No"})
        es_client.indices.refresh(index=es_index)

        resp = es_client.search(
            index=es_index,
            query={"query_string": {"query": "required:Yes", "default_operator": "AND"}},
        )
        assert resp["hits"]["total"]["value"] == 1
        assert resp["hits"]["hits"][0]["_id"] == "1"


# ---------------------------------------------------------------------------
# Textual app integration tests
# ---------------------------------------------------------------------------

class TestAppBasics:
    """Basic app tests that don't require ES."""

    @pytest.mark.asyncio
    async def test_app_loads_file(self):
        app = LinkMLEditor(initial_file=SCHEMA_PATH)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert len(app.fields) > 0
            assert app.current_file == SCHEMA_PATH

    @pytest.mark.asyncio
    async def test_fields_table_populated(self):
        app = LinkMLEditor(initial_file=SCHEMA_PATH)
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#fields-table")
            assert table.row_count > 0

    @pytest.mark.asyncio
    async def test_toggle_groups(self):
        app = LinkMLEditor(initial_file=SCHEMA_PATH)
        async with app.run_test() as pilot:
            await pilot.pause()
            # Ensure the DataTable has focus (not the search Input)
            app.query_one("#fields-table").focus()
            await pilot.pause()
            assert len(app.collapsed_groups) == 0
            await pilot.press("g")
            await pilot.pause()
            assert len(app.collapsed_groups) == 1

    @pytest.mark.asyncio
    async def test_toggle_groups_roundtrip(self):
        app = LinkMLEditor(initial_file=SCHEMA_PATH)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#fields-table").focus()
            await pilot.pause()
            await pilot.press("g")
            await pilot.pause()
            await pilot.press("g")
            await pilot.pause()
            assert len(app.collapsed_groups) == 0

    @pytest.mark.asyncio
    async def test_view_switching(self):
        app = LinkMLEditor(initial_file=SCHEMA_PATH)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#fields-table").focus()
            await pilot.pause()
            assert app.current_view == "fields"
            await pilot.press("e")
            await pilot.pause()
            assert app.current_view == "enums"
            await pilot.press("f")
            await pilot.pause()
            assert app.current_view == "fields"

    @pytest.mark.asyncio
    async def test_undo_redo_stacks_initialized(self):
        app = LinkMLEditor(initial_file=SCHEMA_PATH)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert len(app._undo_stack) == 0
            assert len(app._redo_stack) == 0

    @pytest.mark.asyncio
    async def test_get_row_key_at(self):
        app = LinkMLEditor(initial_file=SCHEMA_PATH)
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#fields-table")
            key = app._get_row_key_at(table, 0)
            assert isinstance(key, str)


# ---------------------------------------------------------------------------
# Textual app + ES search tests
# ---------------------------------------------------------------------------

class TestAppSearch:
    """App-level search tests requiring Elasticsearch.

    Each test creates a LinkMLEditor with ES, runs the test, and cleans up
    the ES index afterwards.
    """

    @es_integration
    @pytest.mark.asyncio
    async def test_es_connected_on_startup(self):
        app = LinkMLEditor(initial_file=SCHEMA_PATH, es_url="http://localhost:9200")
        async with app.run_test() as pilot:
            await pilot.pause()
            try:
                assert app._es is not None
            finally:
                app._cleanup_elasticsearch()

    @es_integration
    @pytest.mark.asyncio
    async def test_fields_indexed_on_load(self):
        app = LinkMLEditor(initial_file=SCHEMA_PATH, es_url="http://localhost:9200")
        async with app.run_test() as pilot:
            await pilot.pause()
            try:
                assert app._es is not None
                assert app._es_dirty is False
                assert app._es.indices.exists(index=app._es_index)
            finally:
                app._cleanup_elasticsearch()

    @es_integration
    @pytest.mark.asyncio
    async def test_search_filters_table(self):
        app = LinkMLEditor(initial_file=SCHEMA_PATH, es_url="http://localhost:9200")
        async with app.run_test() as pilot:
            await pilot.pause()
            try:
                table = app.query_one("#fields-table")
                initial_count = table.row_count

                app._perform_search("required:Yes")
                await pilot.pause()

                filtered_count = table.row_count
                assert filtered_count < initial_count
                assert filtered_count > 0
            finally:
                app._cleanup_elasticsearch()

    @es_integration
    @pytest.mark.asyncio
    async def test_clear_search_restores_all(self):
        app = LinkMLEditor(initial_file=SCHEMA_PATH, es_url="http://localhost:9200")
        async with app.run_test() as pilot:
            await pilot.pause()
            try:
                table = app.query_one("#fields-table")
                initial_count = table.row_count

                app._perform_search("required:Yes")
                await pilot.pause()
                assert table.row_count < initial_count

                app._perform_search("")
                await pilot.pause()
                assert table.row_count == initial_count
            finally:
                app._cleanup_elasticsearch()

    @es_integration
    @pytest.mark.asyncio
    async def test_search_highlights_populated(self):
        app = LinkMLEditor(initial_file=SCHEMA_PATH, es_url="http://localhost:9200")
        async with app.run_test() as pilot:
            await pilot.pause()
            try:
                app._perform_search("sample")
                await pilot.pause()

                assert app._search_matched is not None
                assert len(app._search_matched) > 0
                assert len(app._search_highlights) > 0
            finally:
                app._cleanup_elasticsearch()

    @es_integration
    @pytest.mark.asyncio
    async def test_malformed_query_no_crash(self):
        app = LinkMLEditor(initial_file=SCHEMA_PATH, es_url="http://localhost:9200")
        async with app.run_test() as pilot:
            await pilot.pause()
            try:
                app._perform_search("name:")
                await pilot.pause()

                app._perform_search("")
                await pilot.pause()
                assert app._search_matched is None
            finally:
                app._cleanup_elasticsearch()

    @es_integration
    @pytest.mark.asyncio
    async def test_es_cleanup_on_quit(self):
        app = LinkMLEditor(initial_file=SCHEMA_PATH, es_url="http://localhost:9200")
        async with app.run_test() as pilot:
            await pilot.pause()
            idx = app._es_index
            es = app._es
            assert es is not None
            assert es.indices.exists(index=idx)
            app._cleanup_elasticsearch()
            assert not es.indices.exists(index=idx)

    @es_integration
    @pytest.mark.asyncio
    async def test_name_field_search(self):
        """Test that name:sample works with the underscore analyzer."""
        app = LinkMLEditor(initial_file=SCHEMA_PATH, es_url="http://localhost:9200")
        async with app.run_test() as pilot:
            await pilot.pause()
            try:
                app._perform_search("name:sample")
                await pilot.pause()

                assert app._search_matched is not None
                assert len(app._search_matched) > 0
                for name in app._search_matched:
                    assert "sample" in name.lower()
            finally:
                app._cleanup_elasticsearch()


# ---------------------------------------------------------------------------
# Embrace schema search tests – multi-field search verification
# ---------------------------------------------------------------------------

class TestSearchEmbraceSchema:
    """Search tests using embrace.yaml to verify all fields are queried.

    These tests confirm that search queries match fields based on name, title,
    description, range, slot_group, source, and pattern attributes – not just
    description.
    """

    @es_integration
    @pytest.mark.asyncio
    async def test_search_by_name_collection_date(self):
        """Searching 'collection_date' should match the collection_date field."""
        app = LinkMLEditor(initial_file=EMBRACE_PATH, es_url="http://localhost:9200")
        async with app.run_test() as pilot:
            await pilot.pause()
            try:
                table = app.query_one("#fields-table")
                initial_count = table.row_count

                app._perform_search("collection_date")
                await pilot.pause()

                assert app._search_matched is not None
                assert "collection_date" in app._search_matched
                assert table.row_count < initial_count
                assert table.row_count > 0
            finally:
                app._cleanup_elasticsearch()

    @es_integration
    @pytest.mark.asyncio
    async def test_search_by_name_pathogenicity(self):
        """Searching 'pathogenicity' should match known_pathogenicity via name.

        The word 'pathogenicity' appears in the field name and title but NOT in
        the description (which uses 'pathogenic' instead).  This confirms the
        search inspects the name field, not just description.
        """
        app = LinkMLEditor(initial_file=EMBRACE_PATH, es_url="http://localhost:9200")
        async with app.run_test() as pilot:
            await pilot.pause()
            try:
                app._perform_search("pathogenicity")
                await pilot.pause()

                assert app._search_matched is not None
                assert "known_pathogenicity" in app._search_matched
            finally:
                app._cleanup_elasticsearch()

    @es_integration
    @pytest.mark.asyncio
    async def test_search_by_range(self):
        """Searching 'TrophicLevelMenu' should match trophic_level via range.

        'TrophicLevelMenu' only appears as a range value, never in name, title,
        or description.  This confirms the range field is indexed and queried.
        """
        app = LinkMLEditor(initial_file=EMBRACE_PATH, es_url="http://localhost:9200")
        async with app.run_test() as pilot:
            await pilot.pause()
            try:
                table = app.query_one("#fields-table")
                app._perform_search("TrophicLevelMenu")
                await pilot.pause()

                assert app._search_matched is not None
                assert "trophic_level" in app._search_matched
                assert len(app._search_matched) == 1
                assert table.row_count == 1
            finally:
                app._cleanup_elasticsearch()

    @es_integration
    @pytest.mark.asyncio
    async def test_search_by_source(self):
        """Searching 'source:SRA.experiment' should match SRA experiment fields.

        Source values like 'SRA.experiment' only appear in the source field, so
        a field-prefixed query tests that source is indexed and queryable.
        """
        app = LinkMLEditor(initial_file=EMBRACE_PATH, es_url="http://localhost:9200")
        async with app.run_test() as pilot:
            await pilot.pause()
            try:
                app._perform_search("source:SRA.experiment")
                await pilot.pause()

                assert app._search_matched is not None
                assert len(app._search_matched) >= 10
                assert "TITLE" in app._search_matched
                assert "LIBRARY_STRATEGY" in app._search_matched
            finally:
                app._cleanup_elasticsearch()

    @es_integration
    @pytest.mark.asyncio
    async def test_search_by_slot_group(self):
        """Searching 'ecosystem' should match fields in the ecosystem slot_group.

        'ecosystem' appears only in the slot_group value
        'ERC000022:Organism characteristics: ecosystem', never in name, title,
        or description.  This confirms slot_group is indexed and queried.
        """
        app = LinkMLEditor(initial_file=EMBRACE_PATH, es_url="http://localhost:9200")
        async with app.run_test() as pilot:
            await pilot.pause()
            try:
                app._perform_search("ecosystem")
                await pilot.pause()

                assert app._search_matched is not None
                expected = {
                    "trophic_level",
                    "observed_biotic_relationship",
                    "known_pathogenicity",
                    "relationship_to_oxygen",
                    "propagation",
                }
                assert expected.issubset(app._search_matched)
            finally:
                app._cleanup_elasticsearch()

    @es_integration
    @pytest.mark.asyncio
    async def test_search_no_results(self):
        """Searching for a nonexistent term should return zero matches."""
        app = LinkMLEditor(initial_file=EMBRACE_PATH, es_url="http://localhost:9200")
        async with app.run_test() as pilot:
            await pilot.pause()
            try:
                table = app.query_one("#fields-table")

                app._perform_search("xyznonexistent123")
                await pilot.pause()

                assert app._search_matched is not None
                assert len(app._search_matched) == 0
                assert table.row_count == 0
            finally:
                app._cleanup_elasticsearch()

    @es_integration
    @pytest.mark.asyncio
    async def test_search_single_result_latitude(self):
        """Searching 'latitude' should match geographic_location_latitude."""
        app = LinkMLEditor(initial_file=EMBRACE_PATH, es_url="http://localhost:9200")
        async with app.run_test() as pilot:
            await pilot.pause()
            try:
                app._perform_search("latitude")
                await pilot.pause()

                assert app._search_matched is not None
                assert "geographic_location_latitude" in app._search_matched
                # Only latitude field should match (longitude says 'longitude')
                assert len(app._search_matched) <= 3
            finally:
                app._cleanup_elasticsearch()

    @es_integration
    @pytest.mark.asyncio
    async def test_search_multiple_results_sample(self):
        """Searching 'sample' should match multiple sample-related fields."""
        app = LinkMLEditor(initial_file=EMBRACE_PATH, es_url="http://localhost:9200")
        async with app.run_test() as pilot:
            await pilot.pause()
            try:
                app._perform_search("sample")
                await pilot.pause()

                assert app._search_matched is not None
                assert len(app._search_matched) >= 5
                assert "sample_collection_device" in app._search_matched
                assert "sample_collection_method" in app._search_matched
            finally:
                app._cleanup_elasticsearch()

    @es_integration
    @pytest.mark.asyncio
    async def test_search_filters_table_rows(self):
        """Search should reduce visible table rows to matching fields only."""
        app = LinkMLEditor(initial_file=EMBRACE_PATH, es_url="http://localhost:9200")
        async with app.run_test() as pilot:
            await pilot.pause()
            try:
                table = app.query_one("#fields-table")
                initial_count = table.row_count

                # TrophicLevelMenu only appears as a range value on one field
                app._perform_search("TrophicLevelMenu")
                await pilot.pause()

                assert table.row_count == 1
                assert table.row_count < initial_count
            finally:
                app._cleanup_elasticsearch()

    @es_integration
    @pytest.mark.asyncio
    async def test_search_clear_restores_all_rows(self):
        """Clearing search should restore all rows."""
        app = LinkMLEditor(initial_file=EMBRACE_PATH, es_url="http://localhost:9200")
        async with app.run_test() as pilot:
            await pilot.pause()
            try:
                table = app.query_one("#fields-table")
                initial_count = table.row_count

                app._perform_search("TrophicLevelMenu")
                await pilot.pause()
                assert table.row_count < initial_count

                app._perform_search("")
                await pilot.pause()
                assert table.row_count == initial_count
            finally:
                app._cleanup_elasticsearch()

    @es_integration
    @pytest.mark.asyncio
    async def test_search_by_pattern_field(self):
        """Searching for a pattern substring should match fields with that pattern.

        Fields like microbial_biomass have a numeric regex pattern.  Searching
        for a distinctive pattern fragment confirms the pattern field is queried.
        """
        app = LinkMLEditor(initial_file=EMBRACE_PATH, es_url="http://localhost:9200")
        async with app.run_test() as pilot:
            await pilot.pause()
            try:
                # '[+-]?[0-9]+' is a pattern on number_of_replicons, etc.
                app._perform_search("pattern:0-9")
                await pilot.pause()

                assert app._search_matched is not None
                assert len(app._search_matched) > 0
            finally:
                app._cleanup_elasticsearch()


# ---------------------------------------------------------------------------
# Partial / sub-word matching tests
# ---------------------------------------------------------------------------

class TestPartialMatching:
    """Tests that verify edge-ngram partial matching works correctly.

    Typing a prefix of a word (e.g. 'coll' for 'collection') should match
    fields that contain a token starting with that prefix, across all
    searchable attributes.
    """

    @es_integration
    @pytest.mark.asyncio
    async def test_partial_name_coll(self):
        """'coll' should match fields whose name contains a token starting with 'coll'."""
        app = LinkMLEditor(initial_file=EMBRACE_PATH, es_url="http://localhost:9200")
        async with app.run_test() as pilot:
            await pilot.pause()
            try:
                table = app.query_one("#fields-table")
                initial_count = table.row_count

                app._perform_search("coll")
                await pilot.pause()

                assert app._search_matched is not None
                assert "collection_date" in app._search_matched
                assert "sample_collection_device" in app._search_matched
                assert "sample_collection_method" in app._search_matched
                assert table.row_count < initial_count
                assert table.row_count > 0
            finally:
                app._cleanup_elasticsearch()

    @es_integration
    @pytest.mark.asyncio
    async def test_partial_name_troph(self):
        """'troph' should match trophic_level (prefix of 'trophic')."""
        app = LinkMLEditor(initial_file=EMBRACE_PATH, es_url="http://localhost:9200")
        async with app.run_test() as pilot:
            await pilot.pause()
            try:
                app._perform_search("troph")
                await pilot.pause()

                assert app._search_matched is not None
                assert "trophic_level" in app._search_matched
            finally:
                app._cleanup_elasticsearch()

    @es_integration
    @pytest.mark.asyncio
    async def test_partial_name_patho(self):
        """'patho' should match known_pathogenicity (prefix of 'pathogenicity').

        'patho' does NOT appear as a complete word in any field, confirming
        sub-word / prefix matching is working.
        """
        app = LinkMLEditor(initial_file=EMBRACE_PATH, es_url="http://localhost:9200")
        async with app.run_test() as pilot:
            await pilot.pause()
            try:
                app._perform_search("patho")
                await pilot.pause()

                assert app._search_matched is not None
                assert "known_pathogenicity" in app._search_matched
            finally:
                app._cleanup_elasticsearch()

    @es_integration
    @pytest.mark.asyncio
    async def test_partial_name_lati(self):
        """'lati' should match geographic_location_latitude (prefix of 'latitude')."""
        app = LinkMLEditor(initial_file=EMBRACE_PATH, es_url="http://localhost:9200")
        async with app.run_test() as pilot:
            await pilot.pause()
            try:
                app._perform_search("lati")
                await pilot.pause()

                assert app._search_matched is not None
                assert "geographic_location_latitude" in app._search_matched
            finally:
                app._cleanup_elasticsearch()

    @es_integration
    @pytest.mark.asyncio
    async def test_partial_slot_group_eco(self):
        """'eco' should match fields in the 'ecosystem' slot_group.

        'eco' is a prefix of 'ecosystem' which only appears in the slot_group
        attribute, confirming partial matching works on slot_group.
        """
        app = LinkMLEditor(initial_file=EMBRACE_PATH, es_url="http://localhost:9200")
        async with app.run_test() as pilot:
            await pilot.pause()
            try:
                app._perform_search("eco")
                await pilot.pause()

                assert app._search_matched is not None
                # The 5 fields in "ERC000022:Organism characteristics: ecosystem"
                ecosystem_fields = {
                    "trophic_level",
                    "observed_biotic_relationship",
                    "known_pathogenicity",
                    "relationship_to_oxygen",
                    "propagation",
                }
                assert ecosystem_fields.issubset(app._search_matched)
            finally:
                app._cleanup_elasticsearch()

    @es_integration
    @pytest.mark.asyncio
    async def test_partial_field_specific_name_coll(self):
        """'name:coll' should match fields whose name has a token starting with 'coll'."""
        app = LinkMLEditor(initial_file=EMBRACE_PATH, es_url="http://localhost:9200")
        async with app.run_test() as pilot:
            await pilot.pause()
            try:
                app._perform_search("name:coll")
                await pilot.pause()

                assert app._search_matched is not None
                assert "collection_date" in app._search_matched
                assert "sample_collection_device" in app._search_matched
                # Should NOT match fields that only have "coll" in description
                # but not in name
                assert len(app._search_matched) <= 10
            finally:
                app._cleanup_elasticsearch()

    @es_integration
    @pytest.mark.asyncio
    async def test_partial_range_trophic(self):
        """'range:Trophic' should match trophic_level (range TrophicLevelMenu)."""
        app = LinkMLEditor(initial_file=EMBRACE_PATH, es_url="http://localhost:9200")
        async with app.run_test() as pilot:
            await pilot.pause()
            try:
                app._perform_search("range:Trophic")
                await pilot.pause()

                assert app._search_matched is not None
                assert "trophic_level" in app._search_matched
            finally:
                app._cleanup_elasticsearch()

    @es_integration
    @pytest.mark.asyncio
    async def test_partial_no_match(self):
        """A prefix that matches no token should return zero results."""
        app = LinkMLEditor(initial_file=EMBRACE_PATH, es_url="http://localhost:9200")
        async with app.run_test() as pilot:
            await pilot.pause()
            try:
                table = app.query_one("#fields-table")
                app._perform_search("xyqz")
                await pilot.pause()

                assert app._search_matched is not None
                assert len(app._search_matched) == 0
                assert table.row_count == 0
            finally:
                app._cleanup_elasticsearch()

    @es_integration
    @pytest.mark.asyncio
    async def test_partial_filters_table_rows(self):
        """Partial search should hide non-matching rows and show matching ones."""
        app = LinkMLEditor(initial_file=EMBRACE_PATH, es_url="http://localhost:9200")
        async with app.run_test() as pilot:
            await pilot.pause()
            try:
                table = app.query_one("#fields-table")
                initial_count = table.row_count

                app._perform_search("troph")
                await pilot.pause()

                assert table.row_count >= 1
                assert table.row_count < initial_count

                # Clear and verify restore
                app._perform_search("")
                await pilot.pause()
                assert table.row_count == initial_count
            finally:
                app._cleanup_elasticsearch()

    @es_integration
    @pytest.mark.asyncio
    async def test_partial_source_sra(self):
        """'source:SRA' should match all fields with source starting with 'SRA'."""
        app = LinkMLEditor(initial_file=EMBRACE_PATH, es_url="http://localhost:9200")
        async with app.run_test() as pilot:
            await pilot.pause()
            try:
                app._perform_search("source:SRA")
                await pilot.pause()

                assert app._search_matched is not None
                assert len(app._search_matched) >= 10
                assert "LIBRARY_STRATEGY" in app._search_matched
            finally:
                app._cleanup_elasticsearch()

    @es_integration
    @pytest.mark.asyncio
    async def test_exact_match_still_works(self):
        """Full-word searches should continue to work with partial matching enabled."""
        app = LinkMLEditor(initial_file=EMBRACE_PATH, es_url="http://localhost:9200")
        async with app.run_test() as pilot:
            await pilot.pause()
            try:
                app._perform_search("collection_date")
                await pilot.pause()

                assert app._search_matched is not None
                assert "collection_date" in app._search_matched

                app._perform_search("TrophicLevelMenu")
                await pilot.pause()

                assert app._search_matched is not None
                assert "trophic_level" in app._search_matched
                assert len(app._search_matched) == 1
            finally:
                app._cleanup_elasticsearch()


# ---------------------------------------------------------------------------
# Regexp / single-character and cross-delimiter substring matching tests
# ---------------------------------------------------------------------------

class TestRegexpMatching:
    """Tests that verify regexp-based substring matching.

    The search now uses ES regexp queries on .keyword sub-fields so that:
    - A single letter (e.g. 'm') matches any field containing that letter.
    - Arbitrary substrings including delimiters (e.g. '22:sa') match
      anywhere within a field value (e.g. 'ERC000022:sample collection').
    """

    @es_integration
    @pytest.mark.asyncio
    async def test_single_letter_m_matches_chimera(self):
        """'m' should match chimera_check_software (contains 'm' in name)."""
        app = LinkMLEditor(initial_file=EMBRACE_PATH, es_url="http://localhost:9200")
        async with app.run_test() as pilot:
            await pilot.pause()
            try:
                app._perform_search("m")
                await pilot.pause()

                assert app._search_matched is not None
                assert "chimera_check_software" in app._search_matched
                # Many other fields also contain 'm'
                assert len(app._search_matched) > 10
            finally:
                app._cleanup_elasticsearch()

    @es_integration
    @pytest.mark.asyncio
    async def test_single_letter_z_few_matches(self):
        """'z' should match only fields that contain the letter 'z'."""
        app = LinkMLEditor(initial_file=EMBRACE_PATH, es_url="http://localhost:9200")
        async with app.run_test() as pilot:
            await pilot.pause()
            try:
                app._perform_search("z")
                await pilot.pause()

                assert app._search_matched is not None
                # 'z' is relatively rare; there should be matches but fewer
                # than a common letter like 'm'
                assert len(app._search_matched) > 0
            finally:
                app._cleanup_elasticsearch()

    @es_integration
    @pytest.mark.asyncio
    async def test_cross_delimiter_22_sa(self):
        """'22:sa' should match fields whose slot_group contains 'ERC000022:sample'.

        The colon in '22:sa' is NOT a known field name prefix, so it is
        treated as a literal substring.  This tests matching across the
        ':' delimiter within the slot_group value.
        """
        app = LinkMLEditor(initial_file=EMBRACE_PATH, es_url="http://localhost:9200")
        async with app.run_test() as pilot:
            await pilot.pause()
            try:
                app._perform_search("22:sa")
                await pilot.pause()

                assert app._search_matched is not None
                assert len(app._search_matched) > 0
                # Fields with slot_group 'ERC000022:sample collection' etc.
                assert "amount_or_size_of_sample_collected" in app._search_matched
            finally:
                app._cleanup_elasticsearch()

    @es_integration
    @pytest.mark.asyncio
    async def test_cross_delimiter_22_sample_processing(self):
        """'22:sample processing' should match slot_group 'ERC000022:sample processing'.

        Multi-word query: '22:sample' and 'processing' are separate tokens
        with AND semantics, both must match (possibly in different fields).
        """
        app = LinkMLEditor(initial_file=EMBRACE_PATH, es_url="http://localhost:9200")
        async with app.run_test() as pilot:
            await pilot.pause()
            try:
                app._perform_search("22:sample processing")
                await pilot.pause()

                assert app._search_matched is not None
                assert len(app._search_matched) > 0
            finally:
                app._cleanup_elasticsearch()

    @es_integration
    @pytest.mark.asyncio
    async def test_single_letter_filters_table(self):
        """Single-letter search should filter the table, not show all rows."""
        app = LinkMLEditor(initial_file=EMBRACE_PATH, es_url="http://localhost:9200")
        async with app.run_test() as pilot:
            await pilot.pause()
            try:
                table = app.query_one("#fields-table")
                initial_count = table.row_count

                # 'q' is rare enough to filter significantly
                app._perform_search("q")
                await pilot.pause()

                assert app._search_matched is not None
                assert table.row_count < initial_count
                assert table.row_count >= 0
            finally:
                app._cleanup_elasticsearch()

    @es_integration
    @pytest.mark.asyncio
    async def test_field_specific_regexp(self):
        """'name:chim' should match chimera_check_software via regexp on name."""
        app = LinkMLEditor(initial_file=EMBRACE_PATH, es_url="http://localhost:9200")
        async with app.run_test() as pilot:
            await pilot.pause()
            try:
                app._perform_search("name:chim")
                await pilot.pause()

                assert app._search_matched is not None
                assert "chimera_check_software" in app._search_matched
            finally:
                app._cleanup_elasticsearch()

    @es_integration
    @pytest.mark.asyncio
    async def test_existing_exact_match_still_works(self):
        """Full exact queries like 'TrophicLevelMenu' should still match."""
        app = LinkMLEditor(initial_file=EMBRACE_PATH, es_url="http://localhost:9200")
        async with app.run_test() as pilot:
            await pilot.pause()
            try:
                app._perform_search("TrophicLevelMenu")
                await pilot.pause()

                assert app._search_matched is not None
                assert "trophic_level" in app._search_matched
                assert len(app._search_matched) == 1
            finally:
                app._cleanup_elasticsearch()

    @es_integration
    @pytest.mark.asyncio
    async def test_existing_partial_still_works(self):
        """Partial prefix queries like 'coll' should still match."""
        app = LinkMLEditor(initial_file=EMBRACE_PATH, es_url="http://localhost:9200")
        async with app.run_test() as pilot:
            await pilot.pause()
            try:
                app._perform_search("coll")
                await pilot.pause()

                assert app._search_matched is not None
                assert "collection_date" in app._search_matched
                assert "sample_collection_device" in app._search_matched
            finally:
                app._cleanup_elasticsearch()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
