#!/usr/bin/env python3
"""Tests for ena_common.py — shared ENA submission utilities."""

from __future__ import annotations

import json
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from textwrap import dedent
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from ena_common import (
    _is_metadata_row,
    _match_by_alias_title,
    extract_records_from_json,
    extract_records_from_tabular,
    find_duplicates_by_alias_title,
    get_credentials,
    parse_checklist_units,
    validate_hold_until,
    write_results,
    xml_to_bytes,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def minimal_schema() -> dict[str, Any]:
    return {
        "classes": {
            "dh_interface": {},
            "MyClass": {"is_a": "dh_interface", "slots": ["alias", "title", "status"]},
        },
        "slots": {
            "alias": {"title": "Alias", "required": True},
            "title": {"title": "Title"},
            "status": {"title": "Status", "range": "StatusMenu"},
        },
        "enums": {
            "StatusMenu": {"permissible_values": {"PRIVATE": {}, "PUBLIC": {}}},
        },
    }


# ---------------------------------------------------------------------------
# TestGetCredentials
# ---------------------------------------------------------------------------


class TestGetCredentials:

    def test_returns_username_and_password(self) -> None:
        with patch.dict(os.environ, {"ENA_WEBIN": "Webin-123", "ENA_WEBIN_PASSWORD": "secret"}):
            user, pw = get_credentials()
        assert user == "Webin-123"
        assert pw == "secret"

    def test_strips_whitespace(self) -> None:
        with patch.dict(os.environ, {"ENA_WEBIN": "  Webin-123  ", "ENA_WEBIN_PASSWORD": " pass "}):
            user, pw = get_credentials()
        assert user == "Webin-123"
        assert pw == "pass"

    def test_raises_when_username_missing(self) -> None:
        env = {"ENA_WEBIN_PASSWORD": "pass"}
        with patch.dict(os.environ, env, clear=True):
            os.environ.pop("ENA_WEBIN", None)
            with pytest.raises(ValueError, match="ENA_WEBIN"):
                get_credentials()

    def test_raises_when_password_missing(self) -> None:
        env = {"ENA_WEBIN": "Webin-123"}
        with patch.dict(os.environ, env, clear=True):
            os.environ.pop("ENA_WEBIN_PASSWORD", None)
            with pytest.raises(ValueError, match="ENA_WEBIN_PASSWORD"):
                get_credentials()

    def test_raises_when_both_missing(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("ENA_WEBIN", None)
            os.environ.pop("ENA_WEBIN_PASSWORD", None)
            with pytest.raises(ValueError):
                get_credentials()


# ---------------------------------------------------------------------------
# TestGetBaseUrl
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# TestXmlToBytes
# ---------------------------------------------------------------------------


class TestXmlToBytes:

    def test_returns_bytes(self) -> None:
        root = ET.Element("ROOT")
        assert isinstance(xml_to_bytes(root), bytes)

    def test_includes_xml_declaration(self) -> None:
        root = ET.Element("ROOT")
        result = xml_to_bytes(root).decode()
        assert "<?xml" in result

    def test_round_trips_element(self) -> None:
        root = ET.Element("SAMPLE")
        ET.SubElement(root, "TITLE").text = "hello"
        parsed = ET.fromstring(xml_to_bytes(root))
        assert parsed.find("TITLE").text == "hello"

    def test_utf8_encoding(self) -> None:
        root = ET.Element("ROOT")
        root.text = "café"
        result = xml_to_bytes(root)
        assert b"UTF-8" in result or b"utf-8" in result.lower()


# ---------------------------------------------------------------------------
# TestValidateHoldUntil
# ---------------------------------------------------------------------------


class TestValidateHoldUntil:

    def test_valid_future_date_accepted(self) -> None:
        import pendulum
        within_two_years = pendulum.today().add(years=1).to_date_string()
        date = validate_hold_until(within_two_years)
        assert date is not None

    def test_invalid_format_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid date"):
            validate_hold_until("not-a-date")

    def test_past_date_raises(self) -> None:
        with pytest.raises(ValueError, match="not in the future"):
            validate_hold_until("2000-01-01")

    def test_too_far_future_raises(self) -> None:
        with pytest.raises(ValueError, match="more than 2 years"):
            validate_hold_until("2099-12-31")

    def test_today_raises(self) -> None:
        import pendulum
        today = pendulum.today().date().to_date_string()
        with pytest.raises(ValueError, match="not in the future"):
            validate_hold_until(today)


# ---------------------------------------------------------------------------
# TestFindDuplicates
# ---------------------------------------------------------------------------


class TestFindDuplicates:

    @staticmethod
    def _account(title: str = "", alias: str = "", accession: str = "ACC1") -> dict[str, str]:
        return {"title": title, "alias": alias, "accession": accession, "secondary_accession": "", "status": "PRIVATE"}

    def test_alias_match_detected(self) -> None:
        dups = find_duplicates_by_alias_title(
            [{"TITLE": "X", "alias": "my-alias"}],
            [self._account(alias="my-alias", accession="ACC1")],
            title_field="TITLE", entity_label="records",
        )
        assert 0 in dups
        assert "alias" in dups[0]["match_reason"]

    def test_title_match_detected(self) -> None:
        dups = find_duplicates_by_alias_title(
            [{"TITLE": "My Study"}],
            [self._account(title="My Study", accession="ACC2")],
            title_field="TITLE", entity_label="records",
        )
        assert 0 in dups
        assert "title" in dups[0]["match_reason"]

    def test_no_match_returns_empty(self) -> None:
        dups = find_duplicates_by_alias_title(
            [{"TITLE": "Novel", "alias": "novel"}],
            [self._account(title="Existing", alias="existing")],
            title_field="TITLE", entity_label="records",
        )
        assert dups == {}

    def test_empty_account_returns_empty(self) -> None:
        dups = find_duplicates_by_alias_title([{"TITLE": "X"}], [], "TITLE", "records")
        assert dups == {}

    def test_alias_takes_priority_over_title(self) -> None:
        # Same alias AND title exist in account under different accessions
        account = [
            self._account(alias="my-alias", accession="ACC-ALIAS"),
            self._account(title="My Title", accession="ACC-TITLE"),
        ]
        new = [{"TITLE": "My Title", "alias": "my-alias"}]
        dups = find_duplicates_by_alias_title(new, account, "TITLE", "records")
        assert dups[0]["accession"] == "ACC-ALIAS"


# ---------------------------------------------------------------------------
# TestMatchByAliasTitle
# ---------------------------------------------------------------------------


class TestMatchByAliasTitle:

    def test_alias_match(self) -> None:
        by_alias = {"x": {"accession": "A1", "alias": "x", "title": "", "status": "OK", "secondary_accession": ""}}
        result = _match_by_alias_title("x", "", by_alias, {})
        assert result is not None
        assert result["accession"] == "A1"

    def test_title_match(self) -> None:
        by_title = {"T": {"accession": "A2", "alias": "", "title": "T", "status": "OK", "secondary_accession": ""}}
        result = _match_by_alias_title("", "T", {}, by_title)
        assert result is not None
        assert result["accession"] == "A2"

    def test_no_match_returns_none(self) -> None:
        assert _match_by_alias_title("a", "b", {}, {}) is None


# ---------------------------------------------------------------------------
# TestExtractRecordsFromJson
# ---------------------------------------------------------------------------


class TestExtractRecordsFromJson:

    def test_plain_list(self) -> None:
        result = extract_records_from_json([{"a": "1"}])
        assert result == [{"a": "1"}]

    def test_dict_with_key(self) -> None:
        result = extract_records_from_json({"studies": [{"a": "1"}]}, record_keys=("studies",))
        assert result == [{"a": "1"}]

    def test_container_format(self) -> None:
        data = {"Container": {"SRA_studies": [{"a": "1"}]}}
        result = extract_records_from_json(data)
        assert result == [{"a": "1"}]

    def test_single_dict_wrapped_in_list(self) -> None:
        result = extract_records_from_json({"a": "1"})
        assert result == [{"a": "1"}]

    def test_unrecognised_type_returns_none(self) -> None:
        assert extract_records_from_json("not a dict or list") is None


# ---------------------------------------------------------------------------
# TestExtractRecordsFromTabular
# ---------------------------------------------------------------------------


class TestExtractRecordsFromTabular:

    def test_basic_csv(self, tmp_path: Path) -> None:
        f = tmp_path / "data.csv"
        f.write_text("name,value\nalpha,1\nbeta,2\n")
        result = extract_records_from_tabular(f)
        assert result == [{"name": "alpha", "value": "1"}, {"name": "beta", "value": "2"}]

    def test_tsv_delimiter(self, tmp_path: Path) -> None:
        f = tmp_path / "data.tsv"
        f.write_text("name\tvalue\nalpha\t1\n")
        result = extract_records_from_tabular(f, delimiter="\t")
        assert result[0]["name"] == "alpha"

    def test_metadata_row_skipped(self, tmp_path: Path) -> None:
        f = tmp_path / "data.csv"
        f.write_text("DataHarmonizer v1.0\nname,value\nalpha,1\n")
        result = extract_records_from_tabular(f)
        assert result[0]["name"] == "alpha"

    def test_empty_file_returns_empty_list(self, tmp_path: Path) -> None:
        f = tmp_path / "empty.csv"
        f.write_text("")
        assert extract_records_from_tabular(f) == []

    def test_empty_cells_excluded(self, tmp_path: Path) -> None:
        f = tmp_path / "data.csv"
        f.write_text("name,value\nalpha,\n")
        result = extract_records_from_tabular(f)
        assert result[0] == {"name": "alpha"}


# ---------------------------------------------------------------------------
# TestIsMetadataRow
# ---------------------------------------------------------------------------


class TestIsMetadataRow:

    def test_single_non_empty_cell_is_metadata(self) -> None:
        assert _is_metadata_row(["DataHarmonizer v1", "", "", ""])

    def test_multiple_non_empty_cells_not_metadata(self) -> None:
        assert not _is_metadata_row(["name", "value", "status"])

    def test_all_empty_is_metadata(self) -> None:
        assert _is_metadata_row(["", None, ""])


# ---------------------------------------------------------------------------
# TestParseChecklistUnits
# ---------------------------------------------------------------------------


class TestParseChecklistUnits:

    def test_parses_field_units(self, tmp_path: Path) -> None:
        xml_content = dedent("""\
            <CHECKLIST>
              <FIELD>
                <NAME>latitude</NAME>
                <UNITS><UNIT>DD</UNIT></UNITS>
              </FIELD>
              <FIELD>
                <NAME>depth</NAME>
                <UNITS><UNIT>m</UNIT></UNITS>
              </FIELD>
            </CHECKLIST>
        """)
        f = tmp_path / "checklist.xml"
        f.write_text(xml_content)
        result = parse_checklist_units(f)
        assert result["latitude"] == "DD"
        assert result["depth"] == "m"

    def test_field_without_units_omitted(self, tmp_path: Path) -> None:
        xml_content = "<CHECKLIST><FIELD><NAME>no_unit</NAME></FIELD></CHECKLIST>"
        f = tmp_path / "checklist.xml"
        f.write_text(xml_content)
        assert parse_checklist_units(f) == {}

    def test_malformed_xml_returns_empty(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.xml"
        f.write_text("<unclosed")
        assert parse_checklist_units(f) == {}


# ---------------------------------------------------------------------------
# TestWriteResults
# ---------------------------------------------------------------------------


class TestWriteResults:

    def test_writes_to_file(self, tmp_path: Path) -> None:
        out = tmp_path / "results.json"
        write_results({"submitted": [], "failed": []}, out)
        data = json.loads(out.read_text())
        assert "submitted" in data

    def test_writes_to_stdout(self, capsys: Any) -> None:
        write_results({"submitted": [{"accession": "ACC1"}]}, None)
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["submitted"][0]["accession"] == "ACC1"

    def test_output_is_valid_json(self, tmp_path: Path) -> None:
        out = tmp_path / "out.json"
        write_results({"submitted": [], "modified": [], "failed": []}, out)
        json.loads(out.read_text())  # should not raise
