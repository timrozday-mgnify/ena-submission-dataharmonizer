#!/usr/bin/env python3
"""Tests for edit_linkml.py interactive LinkML schema editor."""

import asyncio
import pytest
from edit_linkml import LinkMLEditor, extract_fields, extract_enums


class TestDataExtraction:
    """Tests for data extraction functions."""

    def test_extract_fields(self):
        """Test that fields are extracted correctly from schema."""
        import yaml
        with open("../schemas/ERC000015.yaml", "r") as f:
            schema = yaml.safe_load(f)

        fields = extract_fields(schema)
        assert len(fields) > 0
        assert all("name" in f for f in fields)
        assert all("slot_group" in f for f in fields)

    def test_extract_enums(self):
        """Test that enums are extracted correctly from schema."""
        import yaml
        with open("../schemas/ERC000015.yaml", "r") as f:
            schema = yaml.safe_load(f)

        enums = extract_enums(schema)
        assert len(enums) > 0
        assert all("enum_name" in e for e in enums)
        assert all("value" in e for e in enums)


class TestToggleGroups:
    """Tests for toggle groups functionality."""

    @pytest.mark.asyncio
    async def test_toggle_groups_adds_to_collapsed(self):
        """Test that pressing 'g' adds group to collapsed_groups."""
        app = LinkMLEditor(initial_file="../schemas/ERC000015.yaml")
        async with app.run_test() as pilot:
            await pilot.pause()

            assert len(app.collapsed_groups) == 0

            # Press 'g' to toggle groups
            await pilot.press("g")
            await pilot.pause()

            assert len(app.collapsed_groups) == 1

    @pytest.mark.asyncio
    async def test_toggle_groups_removes_from_collapsed(self):
        """Test that pressing 'g' twice removes group from collapsed_groups."""
        app = LinkMLEditor(initial_file="../schemas/ERC000015.yaml")
        async with app.run_test() as pilot:
            await pilot.pause()

            # Press 'g' twice
            await pilot.press("g")
            await pilot.pause()
            await pilot.press("g")
            await pilot.pause()

            assert len(app.collapsed_groups) == 0

    @pytest.mark.asyncio
    async def test_get_row_key_at_returns_correct_key(self):
        """Test that _get_row_key_at returns the field name, not row data."""
        app = LinkMLEditor(initial_file="../schemas/ERC000015.yaml")
        async with app.run_test() as pilot:
            await pilot.pause()

            fields_table = app.query_one("#fields-table")
            row_key = app._get_row_key_at(fields_table, 0)

            # Should be a string field name, not a list
            assert isinstance(row_key, str)
            assert row_key == app.fields[0]["name"]


class TestViewSwitching:
    """Tests for view switching functionality."""

    @pytest.mark.asyncio
    async def test_switch_to_enums_view(self):
        """Test switching from fields to enums view."""
        app = LinkMLEditor(initial_file="../schemas/ERC000015.yaml")
        async with app.run_test() as pilot:
            await pilot.pause()

            assert app.current_view == "fields"

            await pilot.press("e")
            await pilot.pause()

            assert app.current_view == "enums"

    @pytest.mark.asyncio
    async def test_switch_to_fields_view(self):
        """Test switching from enums to fields view."""
        app = LinkMLEditor(initial_file="../schemas/ERC000015.yaml")
        async with app.run_test() as pilot:
            await pilot.pause()

            await pilot.press("e")
            await pilot.pause()
            await pilot.press("f")
            await pilot.pause()

            assert app.current_view == "fields"

    @pytest.mark.asyncio
    async def test_switch_to_enums_jumps_to_enum(self):
        """Test that switching to enums on a field with enum range jumps to that enum."""
        app = LinkMLEditor(initial_file="../schemas/ERC000015.yaml")
        async with app.run_test() as pilot:
            await pilot.pause()

            # First field has range 'TrophicLevelMenu' which is an enum
            fields_table = app.query_one("#fields-table")
            first_field = app.fields[0]
            assert first_field["range"] == "TrophicLevelMenu"

            await pilot.press("e")
            await pilot.pause()

            # Should be at an enum with name 'TrophicLevelMenu'
            enums_table = app.query_one("#enums-table")
            if enums_table.cursor_row is not None:
                row_key = app._get_row_key_at(enums_table, enums_table.cursor_row)
                assert row_key is not None
                assert "TrophicLevelMenu" in row_key


class TestUndoRedo:
    """Tests for undo/redo functionality."""

    @pytest.mark.asyncio
    async def test_undo_stack_initialized(self):
        """Test that undo stack is initialized."""
        app = LinkMLEditor(initial_file="../schemas/ERC000015.yaml")
        async with app.run_test() as pilot:
            await pilot.pause()

            assert hasattr(app, "_undo_stack")
            assert hasattr(app, "_redo_stack")
            assert len(app._undo_stack) == 0
            assert len(app._redo_stack) == 0


if __name__ == "__main__":
    # Run tests without pytest
    import yaml

    print("Running tests...")

    # Test data extraction
    print("\n=== Testing data extraction ===")
    with open("../schemas/ERC000015.yaml", "r") as f:
        schema = yaml.safe_load(f)

    fields = extract_fields(schema)
    print(f"Extracted {len(fields)} fields")
    assert len(fields) > 0

    enums = extract_enums(schema)
    print(f"Extracted {len(enums)} enum values")
    assert len(enums) > 0

    # Test toggle groups
    print("\n=== Testing toggle groups ===")

    async def test_toggle():
        app = LinkMLEditor(initial_file="../schemas/ERC000015.yaml")
        async with app.run_test() as pilot:
            await pilot.pause()

            fields_table = app.query_one("#fields-table")

            # Test _get_row_key_at returns string, not list
            row_key = app._get_row_key_at(fields_table, 0)
            assert isinstance(row_key, str), f"Expected str, got {type(row_key)}"
            print(f"  _get_row_key_at returns string: {row_key}")

            # Test toggle
            assert len(app.collapsed_groups) == 0
            await pilot.press("g")
            await pilot.pause()
            assert len(app.collapsed_groups) == 1
            print(f"  Toggle adds group: {app.collapsed_groups}")

            await pilot.press("g")
            await pilot.pause()
            assert len(app.collapsed_groups) == 0
            print("  Toggle removes group")

    asyncio.run(test_toggle())
    print("Toggle groups tests PASSED")

    # Test view switching
    print("\n=== Testing view switching ===")

    async def test_views():
        app = LinkMLEditor(initial_file="../schemas/ERC000015.yaml")
        async with app.run_test() as pilot:
            await pilot.pause()

            assert app.current_view == "fields"
            await pilot.press("e")
            await pilot.pause()
            assert app.current_view == "enums"
            print("  Switch to enums: OK")

            await pilot.press("f")
            await pilot.pause()
            assert app.current_view == "fields"
            print("  Switch to fields: OK")

    asyncio.run(test_views())
    print("View switching tests PASSED")

    print("\n=== ALL TESTS PASSED ===")
