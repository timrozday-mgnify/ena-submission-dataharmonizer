#!/usr/bin/env python3
"""Tests for submit_study.py — JSON extraction and LinkML validation.

Usage:
    pytest scripts/test_submit_study.py -v
"""

import json
import os
import sys

import pytest
import yaml

# Ensure the scripts directory is importable
sys.path.insert(0, os.path.dirname(__file__))

from unittest.mock import patch

from requests.auth import HTTPBasicAuth

from submit_study import (
    extract_studies_from_json,
    extract_studies_from_tabular,
    extract_studies_from_excel,
    load_input_file,
    load_linkml_schema,
    validate_against_linkml,
    build_submission_xml,
    xml_to_bytes,
    validate_against_xsd,
    find_duplicate_studies,
)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "..", "assets", "test-fixtures")
SCHEMAS_DIR = os.path.join(os.path.dirname(__file__), "..", "schemas")
MIMICC_JSON = os.path.join(FIXTURES_DIR, "mimicc_study.json")
MIMICC_CSV = os.path.join(FIXTURES_DIR, "mimicc_study.csv")
MIMICC_TSV = os.path.join(FIXTURES_DIR, "mimicc_study.tsv")
MIMICC_XLSX = os.path.join(FIXTURES_DIR, "mimicc_study.xlsx")
MIMICC_XLS = os.path.join(FIXTURES_DIR, "mimicc_study.xls")
SRA_STUDY_YAML = os.path.join(SCHEMAS_DIR, "SRA_study.yaml")
SRA_STUDY_XSD_DIR = os.path.join(
    os.path.dirname(__file__), "..", "assets", "ena_schema"
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mimicc_json():
    with open(MIMICC_JSON) as f:
        return json.load(f)


@pytest.fixture
def sra_study_schema():
    return load_linkml_schema(SRA_STUDY_YAML)


# ---------------------------------------------------------------------------
# extract_studies_from_json tests
# ---------------------------------------------------------------------------


class TestExtractStudiesFromJson:
    """Tests for extracting study rows from various JSON formats."""

    def test_dataharmonizer_container_format(self, mimicc_json):
        """The mimicc_study.json fixture uses DataHarmonizer Container format."""
        studies = extract_studies_from_json(mimicc_json)
        assert studies is not None
        assert len(studies) == 1
        assert studies[0]["STUDY_TITLE"] == "MIMICC"
        assert studies[0]["existing_study_type"] == "Metagenomics"
        assert studies[0]["IS_PRIMARY"] == "YES"

    def test_plain_list(self):
        data = [{"STUDY_TITLE": "Test Study", "IS_PRIMARY": "YES"}]
        studies = extract_studies_from_json(data)
        assert studies == data

    def test_dict_with_studies_key(self):
        data = {"studies": [{"STUDY_TITLE": "A"}, {"STUDY_TITLE": "B"}]}
        studies = extract_studies_from_json(data)
        assert len(studies) == 2

    def test_dict_with_data_key(self):
        data = {"data": [{"STUDY_TITLE": "C"}]}
        studies = extract_studies_from_json(data)
        assert len(studies) == 1

    def test_single_study_object(self):
        data = {"STUDY_TITLE": "Single"}
        studies = extract_studies_from_json(data)
        assert len(studies) == 1
        assert studies[0]["STUDY_TITLE"] == "Single"

    def test_invalid_input(self):
        assert extract_studies_from_json("not a dict or list") is None

    def test_container_with_multiple_studies(self):
        data = {
            "Container": {
                "SRA_studys": [
                    {"STUDY_TITLE": "Study A"},
                    {"STUDY_TITLE": "Study B"},
                ]
            }
        }
        studies = extract_studies_from_json(data)
        assert len(studies) == 2


# ---------------------------------------------------------------------------
# LinkML validation tests
# ---------------------------------------------------------------------------


class TestValidateAgainstLinkml:
    """Tests for LinkML schema validation."""

    def test_mimicc_study_passes(self, mimicc_json, sra_study_schema):
        """The mimicc_study.json fixture should pass LinkML validation."""
        studies = extract_studies_from_json(mimicc_json)
        is_valid, messages = validate_against_linkml(studies, sra_study_schema)
        # Log messages for debugging
        for msg in messages:
            print(msg)
        assert is_valid, f"Validation failed:\n" + "\n".join(
            m for m in messages if "ERROR" in m
        )

    def test_boolean_yes_no_accepted(self, sra_study_schema):
        """DataHarmonizer exports booleans as YES/NO strings."""
        studies = [
            {
                "IS_PRIMARY": "YES",
                "STUDY_TITLE": "Test",
                "existing_study_type": "Metagenomics",
            }
        ]
        is_valid, messages = validate_against_linkml(studies, sra_study_schema)
        assert is_valid

    def test_boolean_true_false_accepted(self, sra_study_schema):
        studies = [
            {
                "IS_PRIMARY": True,
                "STUDY_TITLE": "Test",
                "existing_study_type": "Metagenomics",
            }
        ]
        is_valid, messages = validate_against_linkml(studies, sra_study_schema)
        assert is_valid

    def test_boolean_string_true_accepted(self, sra_study_schema):
        studies = [
            {
                "IS_PRIMARY": "true",
                "STUDY_TITLE": "Test",
                "existing_study_type": "Metagenomics",
            }
        ]
        is_valid, messages = validate_against_linkml(studies, sra_study_schema)
        assert is_valid

    def test_boolean_invalid_rejected(self, sra_study_schema):
        studies = [
            {
                "IS_PRIMARY": "maybe",
                "STUDY_TITLE": "Test",
                "existing_study_type": "Metagenomics",
            }
        ]
        is_valid, messages = validate_against_linkml(studies, sra_study_schema)
        assert not is_valid

    def test_missing_required_field(self, sra_study_schema):
        """Missing STUDY_TITLE should fail."""
        studies = [
            {
                "IS_PRIMARY": "YES",
                "existing_study_type": "Metagenomics",
            }
        ]
        is_valid, messages = validate_against_linkml(studies, sra_study_schema)
        assert not is_valid
        error_msgs = [m for m in messages if "ERROR" in m]
        assert any("STUDY_TITLE" in m for m in error_msgs)

    def test_invalid_enum_value(self, sra_study_schema):
        studies = [
            {
                "IS_PRIMARY": "YES",
                "STUDY_TITLE": "Test",
                "existing_study_type": "Not A Real Type",
            }
        ]
        is_valid, messages = validate_against_linkml(studies, sra_study_schema)
        assert not is_valid
        error_msgs = [m for m in messages if "ERROR" in m]
        assert any("existing_study_type" in m for m in error_msgs)

    def test_valid_enum_values(self, sra_study_schema):
        """All existing_study_type enum values should be accepted."""
        for study_type in [
            "Whole Genome Sequencing",
            "Metagenomics",
            "RNASeq",
            "Other",
        ]:
            studies = [
                {
                    "IS_PRIMARY": "NO",
                    "STUDY_TITLE": "Test",
                    "existing_study_type": study_type,
                }
            ]
            is_valid, messages = validate_against_linkml(studies, sra_study_schema)
            assert is_valid, f"'{study_type}' should be valid but got: " + "\n".join(
                m for m in messages if "ERROR" in m
            )

    def test_optional_fields_accepted(self, sra_study_schema):
        """Optional fields should not cause validation errors."""
        studies = [
            {
                "IS_PRIMARY": "YES",
                "STUDY_TITLE": "Full Study",
                "existing_study_type": "Metagenomics",
                "STUDY_ABSTRACT": "An abstract.",
                "STUDY_DESCRIPTION": "A description.",
                "CENTER_PROJECT_NAME": "MyProject",
            }
        ]
        is_valid, messages = validate_against_linkml(studies, sra_study_schema)
        assert is_valid

    def test_no_main_class(self):
        """Schema without dh_interface should fail gracefully."""
        schema = {"classes": {"Foo": {"name": "Foo"}}, "slots": {}}
        is_valid, messages = validate_against_linkml([{}], schema)
        assert not is_valid
        assert any("dh_interface" in m for m in messages)


# ---------------------------------------------------------------------------
# XML building tests
# ---------------------------------------------------------------------------


class TestBuildSubmissionXml:
    def test_basic_xml_structure(self):
        studies = [
            {
                "alias": "test-study",
                "STUDY_TITLE": "Test Study",
                "STUDY_ABSTRACT": "Abstract text.",
                "existing_study_type": "Metagenomics",
            }
        ]
        root = build_submission_xml(studies)
        xml_bytes = xml_to_bytes(root)
        xml_str = xml_bytes.decode("utf-8")
        assert "<PROJECT_SET>" in xml_str
        assert 'alias="test-study"' in xml_str
        assert "<TITLE>Test Study</TITLE>" in xml_str
        assert "<DESCRIPTION>Abstract text.</DESCRIPTION>" in xml_str
        assert "<SEQUENCING_PROJECT" in xml_str

    def test_hold_until_date(self):
        studies = [{"STUDY_TITLE": "T", "alias": "a"}]
        root = build_submission_xml(studies, hold_until="2028-01-01")
        xml_bytes = xml_to_bytes(root)
        xml_str = xml_bytes.decode("utf-8")
        assert 'HoldUntilDate="2028-01-01"' in xml_str

    def test_xsd_basic_validation(self):
        """Built XML should pass basic structural validation."""
        studies = [
            {
                "alias": "xsd-test",
                "STUDY_TITLE": "XSD Test Study",
                "existing_study_type": "RNASeq",
            }
        ]
        root = build_submission_xml(studies)
        xml_bytes = xml_to_bytes(root)
        is_valid, messages = validate_against_xsd(xml_bytes, SRA_STUDY_XSD_DIR)
        for msg in messages:
            print(msg)
        assert is_valid


# ---------------------------------------------------------------------------
# Duplicate detection tests
# ---------------------------------------------------------------------------


class TestFindDuplicateStudies:
    """Tests for duplicate detection: public queries first, then private check."""

    MOCK_AUTH = HTTPBasicAuth("Webin-00000", "test")

    @patch("submit_study.search_study_by_title")
    def test_no_duplicates(self, mock_search):
        mock_search.return_value = []
        new = [{"STUDY_TITLE": "New Study", "alias": "new-1"}]
        dups = find_duplicate_studies(new, self.MOCK_AUTH, private_studies=[])
        assert len(dups) == 0
        mock_search.assert_called_once_with("New Study", self.MOCK_AUTH)

    @patch("submit_study.search_study_by_title")
    def test_public_duplicate_by_title(self, mock_search):
        """Public studies found via Portal API are flagged as duplicates."""
        mock_search.return_value = [
            {
                "study_accession": "PRJEB99",
                "secondary_study_accession": "ERP123",
                "study_title": "Existing Study",
                "study_alias": "ex-1",
            }
        ]
        new = [{"STUDY_TITLE": "Existing Study"}]
        dups = find_duplicate_studies(new, self.MOCK_AUTH, private_studies=[])
        assert 0 in dups
        assert dups[0]["accession"] == "PRJEB99"
        assert dups[0]["status"] == "PUBLIC"

    @patch("submit_study.search_study_by_title")
    def test_private_duplicate_by_title(self, mock_search):
        """Private studies matched by title in phase 2 after no public match."""
        mock_search.return_value = []  # No public match
        private = [
            {
                "title": "My Private Study",
                "alias": "priv-1",
                "accession": "PRJEB50",
                "secondary_accession": "ERP50",
                "status": "PRIVATE",
            }
        ]
        new = [{"STUDY_TITLE": "My Private Study"}]
        dups = find_duplicate_studies(new, self.MOCK_AUTH, private_studies=private)
        assert 0 in dups
        assert dups[0]["accession"] == "PRJEB50"
        assert dups[0]["status"] == "PRIVATE"
        assert "private" in dups[0]["match_reason"]

    @patch("submit_study.search_study_by_title")
    def test_private_duplicate_by_alias(self, mock_search):
        """Private studies matched by alias in phase 2."""
        mock_search.return_value = []  # No public match
        private = [
            {
                "title": "Different Title",
                "alias": "my-alias",
                "accession": "PRJEB60",
                "secondary_accession": "",
                "status": "PRIVATE",
            }
        ]
        new = [{"STUDY_TITLE": "New Title", "alias": "my-alias"}]
        dups = find_duplicate_studies(new, self.MOCK_AUTH, private_studies=private)
        assert 0 in dups
        assert dups[0]["accession"] == "PRJEB60"
        assert "alias" in dups[0]["match_reason"]

    @patch("submit_study.search_study_by_title")
    def test_public_match_skips_private_check(self, mock_search):
        """When public match is found, that study isn't rechecked in phase 2."""
        mock_search.return_value = [
            {
                "study_accession": "PRJEB99",
                "study_title": "Already Public",
                "study_alias": "pub-1",
            }
        ]
        private = [
            {
                "title": "Already Public",
                "alias": "pub-1",
                "accession": "PRJEB50",
                "secondary_accession": "",
                "status": "PRIVATE",
            }
        ]
        new = [{"STUDY_TITLE": "Already Public", "alias": "pub-1"}]
        dups = find_duplicate_studies(new, self.MOCK_AUTH, private_studies=private)
        assert 0 in dups
        # Public match takes precedence (found first)
        assert dups[0]["status"] == "PUBLIC"
        assert dups[0]["accession"] == "PRJEB99"

    @patch("submit_study.search_study_by_title")
    def test_partial_match_not_duplicate(self, mock_search):
        """Partial title matches from the Portal API should not count."""
        mock_search.return_value = [
            {
                "study_accession": "PRJEB50",
                "study_title": "My Study Extended Title",
                "study_alias": "other",
            }
        ]
        new = [{"STUDY_TITLE": "My Study"}]
        dups = find_duplicate_studies(new, self.MOCK_AUTH, private_studies=[])
        assert len(dups) == 0

    @patch("submit_study.search_study_by_title")
    def test_mixed_private_public_and_new(self, mock_search):
        """Mix of public duplicate, private duplicate, and new study."""
        private = [
            {
                "title": "Private Dup",
                "alias": "priv-dup",
                "accession": "PRJEB10",
                "secondary_accession": "",
                "status": "PRIVATE",
            }
        ]

        def side_effect(title, auth):
            if title == "Public Dup":
                return [
                    {
                        "study_accession": "PRJEB20",
                        "study_title": "Public Dup",
                        "study_alias": "pub-dup",
                    }
                ]
            return []

        mock_search.side_effect = side_effect
        new = [
            {"STUDY_TITLE": "Private Dup", "alias": "priv-dup"},
            {"STUDY_TITLE": "Public Dup", "alias": "pub-dup"},
            {"STUDY_TITLE": "Brand New", "alias": "new-1"},
        ]
        dups = find_duplicate_studies(new, self.MOCK_AUTH, private_studies=private)
        assert 0 in dups  # private (no public match, caught in phase 2)
        assert 1 in dups  # public (caught in phase 1)
        assert 2 not in dups  # new

    @patch("submit_study.search_study_by_title")
    def test_early_exit_all_public_duplicates(self, mock_search):
        """Stop querying once all studies are found to be duplicates in phase 1."""
        call_count = 0

        def side_effect(title, auth):
            nonlocal call_count
            call_count += 1
            return [
                {
                    "study_accession": f"PRJEB{call_count}",
                    "study_title": title,
                    "study_alias": f"alias-{call_count}",
                }
            ]

        mock_search.side_effect = side_effect
        new = [
            {"STUDY_TITLE": "Study A"},
            {"STUDY_TITLE": "Study B"},
            {"STUDY_TITLE": "Study C"},
        ]
        dups = find_duplicate_studies(new, self.MOCK_AUTH, private_studies=[])
        assert len(dups) == 3
        # All three should have been queried (each found as dup)
        assert call_count == 3

    @patch("submit_study.search_study_by_title")
    def test_early_exit_skips_remaining_queries(self, mock_search):
        """When first N studies are all dups, remaining aren't queried."""
        call_count = 0

        def side_effect(title, auth):
            nonlocal call_count
            call_count += 1
            # Only "Study A" has a match
            if title == "Study A":
                return [{"study_accession": "PRJEB1", "study_title": "Study A", "study_alias": "a"}]
            return []

        mock_search.side_effect = side_effect
        # Single study that's a dup — should return after one query
        new = [{"STUDY_TITLE": "Study A"}]
        dups = find_duplicate_studies(new, self.MOCK_AUTH, private_studies=[])
        assert len(dups) == 1
        assert call_count == 1

    @patch("submit_study.search_study_by_title")
    def test_empty_title_skipped(self, mock_search):
        """Studies with no title or alias should not trigger any query."""
        new = [{}]
        dups = find_duplicate_studies(new, self.MOCK_AUTH, private_studies=[])
        assert len(dups) == 0
        mock_search.assert_not_called()


# ---------------------------------------------------------------------------
# Tabular file loading tests (CSV, TSV, XLS, XLSX)
# ---------------------------------------------------------------------------

# The expected study data shared by all tabular fixtures
EXPECTED_STUDY = {
    "IS_PRIMARY": "YES",
    "STUDY_TITLE": "MIMICC",
    "existing_study_type": "Metagenomics",
}


class TestLoadInputFile:
    """Tests for loading study data from CSV, TSV, XLS, and XLSX files."""

    def test_load_csv(self):
        studies = load_input_file(MIMICC_CSV)
        assert studies is not None
        assert len(studies) == 1
        for key, val in EXPECTED_STUDY.items():
            assert studies[0][key] == val

    def test_load_tsv(self):
        studies = load_input_file(MIMICC_TSV)
        assert studies is not None
        assert len(studies) == 1
        for key, val in EXPECTED_STUDY.items():
            assert studies[0][key] == val

    def test_load_xlsx(self):
        studies = load_input_file(MIMICC_XLSX)
        assert studies is not None
        assert len(studies) == 1
        for key, val in EXPECTED_STUDY.items():
            assert studies[0][key] == val

    def test_load_xls(self):
        studies = load_input_file(MIMICC_XLS)
        assert studies is not None
        assert len(studies) == 1
        for key, val in EXPECTED_STUDY.items():
            assert studies[0][key] == val

    def test_load_json(self):
        studies = load_input_file(MIMICC_JSON)
        assert studies is not None
        assert len(studies) == 1
        for key, val in EXPECTED_STUDY.items():
            assert studies[0][key] == val

    def test_all_formats_produce_same_data(self):
        """All tabular formats should produce the same core study fields."""
        json_studies = load_input_file(MIMICC_JSON)
        csv_studies = load_input_file(MIMICC_CSV)
        tsv_studies = load_input_file(MIMICC_TSV)
        xlsx_studies = load_input_file(MIMICC_XLSX)
        xls_studies = load_input_file(MIMICC_XLS)

        # All should have the same expected keys/values
        for studies in [json_studies, csv_studies, tsv_studies, xlsx_studies, xls_studies]:
            assert len(studies) == 1
            for key, val in EXPECTED_STUDY.items():
                assert studies[0][key] == val

    def test_unknown_extension_returns_none(self, tmp_path):
        unknown = tmp_path / "data.parquet"
        unknown.write_text("dummy")
        result = load_input_file(str(unknown))
        assert result is None

    def test_csv_without_metadata_row(self, tmp_path):
        """A CSV with no metadata row (headers on first line) should still work."""
        csvfile = tmp_path / "no_meta.csv"
        csvfile.write_text("STUDY_TITLE,IS_PRIMARY\nTest,YES\n")
        studies = load_input_file(str(csvfile))
        assert len(studies) == 1
        assert studies[0]["STUDY_TITLE"] == "Test"
        assert studies[0]["IS_PRIMARY"] == "YES"

    def test_tabular_empty_values_omitted(self, tmp_path):
        """Empty cells in tabular files should be omitted from study dicts."""
        csvfile = tmp_path / "sparse.csv"
        csvfile.write_text("STUDY_TITLE,STUDY_ABSTRACT,IS_PRIMARY\nTest,,YES\n")
        studies = load_input_file(str(csvfile))
        assert len(studies) == 1
        assert "STUDY_ABSTRACT" not in studies[0]
        assert studies[0]["STUDY_TITLE"] == "Test"
