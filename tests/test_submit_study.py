#!/usr/bin/env python3
"""Tests for submit_study.py — ENA study submission pipeline.

Covers:
    A. Unit tests for build_submission_xml and _add_project_element
    B. Unit tests for build_manifest
    C. Unit tests for validate_manifest
    D. Unit tests for parse_xml_receipt
    E. Unit tests for find_duplicate_studies and fetch_account_studies
    F. CLI integration tests for main() using typer.testing.CliRunner

Usage:
    pytest test_submit_study.py -v
"""

from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from textwrap import dedent
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner
from requests.auth import HTTPBasicAuth

sys.path.insert(0, str(Path(__file__).parent / "scripts"))

from submit_study import (  # noqa: E402
    _normalize_study_report,
    app,
    build_manifest,
    build_submission_xml,
    fetch_account_studies,
    find_duplicate_studies,
    parse_xml_receipt,
    submit_manifest,
    validate_manifest,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PROD_REPORTS_URL = "https://www.ebi.ac.uk/ena/submit/report/projects"
_TEST_REPORTS_URL = "https://wwwdev.ebi.ac.uk/ena/submit/report/projects"
_REAL_XSD_DIR = str(Path(__file__).parent / "assets" / "ena_schema")

_MINIMAL_SCHEMA_YAML = """\
id: https://example.com/study
name: study_schema
prefixes:
  linkml: https://linkml.io/linkml-model/meta/
imports:
  - linkml:types
classes:
  dh_interface:
    description: DataHarmonizer interface class
  SRA_study:
    is_a: dh_interface
    slots: []
slots: {}
"""

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def basic_study() -> dict[str, Any]:
    return {
        "alias": "test-study-001",
        "STUDY_TITLE": "A Basic Test Study",
        "STUDY_ABSTRACT": "An abstract for the test study.",
        "CENTER_PROJECT_NAME": "My Centre Project",
        "existing_study_type": "Metagenomics",
    }


@pytest.fixture
def metagenomics_assembly_study() -> dict[str, Any]:
    return {
        "alias": "metagenome-assembly-001",
        "STUDY_TITLE": "Primary Metagenome Assembly of Soil Sample",
        "STUDY_ABSTRACT": "Assembly of contigs from metagenome sequencing of soil.",
        "CENTER_PROJECT_NAME": "Soil Metagenome Project",
        "existing_study_type": "Metagenomics",
    }


@pytest.fixture
def mag_genome_study() -> dict[str, Any]:
    return {
        "alias": "mag-genome-001",
        "STUDY_TITLE": "Metagenome-Assembled Genome from Soil Microbiome",
        "STUDY_ABSTRACT": "A high-quality MAG reconstructed from binned metagenome data.",
        "existing_study_type": "Other",
        "new_study_type": "Genome Sequencing",
    }


@pytest.fixture
def auth() -> HTTPBasicAuth:
    return HTTPBasicAuth("Webin-12345", "pass")


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def real_xsd_dir() -> Path:
    return Path(_REAL_XSD_DIR)


# ---------------------------------------------------------------------------
# A. Unit tests for build_submission_xml
# ---------------------------------------------------------------------------


class TestBuildSubmissionXml:

    @staticmethod
    def _to_str(root: ET.Element) -> str:
        return ET.tostring(root, encoding="unicode")

    def test_study_title_round_trips(self, basic_study: dict[str, Any]) -> None:
        root = build_submission_xml([basic_study])
        title_el = root.find(".//TITLE")
        assert title_el is not None
        assert title_el.text == basic_study["STUDY_TITLE"]

    def test_study_abstract_round_trips(self, basic_study: dict[str, Any]) -> None:
        root = build_submission_xml([basic_study])
        desc_el = root.find(".//DESCRIPTION")
        assert desc_el is not None
        assert desc_el.text == basic_study["STUDY_ABSTRACT"]

    def test_alias_round_trips(self, basic_study: dict[str, Any]) -> None:
        root = build_submission_xml([basic_study])
        project_el = root.find(".//PROJECT")
        assert project_el is not None
        assert project_el.get("alias") == basic_study["alias"]

    def test_center_project_name_round_trips(self, basic_study: dict[str, Any]) -> None:
        root = build_submission_xml([basic_study])
        name_el = root.find(".//NAME")
        assert name_el is not None
        assert name_el.text == basic_study["CENTER_PROJECT_NAME"]

    def test_submission_project_present(self, basic_study: dict[str, Any]) -> None:
        root = build_submission_xml([basic_study])
        sp_el = root.find(".//SUBMISSION_PROJECT")
        assert sp_el is not None
        assert sp_el.find("SEQUENCING_PROJECT") is not None

    def test_existing_study_type_emitted_as_project_attribute(self, basic_study: dict[str, Any]) -> None:
        root = build_submission_xml([basic_study])
        xml_str = self._to_str(root)
        assert "existing_study_type" in xml_str
        assert basic_study["existing_study_type"] in xml_str

    def test_new_study_type_absent_when_not_other(self, basic_study: dict[str, Any]) -> None:
        study = dict(basic_study)
        study["new_study_type"] = "Genome Sequencing"
        root = build_submission_xml([study])
        assert "new_study_type" not in self._to_str(root)

    def test_new_study_type_present_when_existing_is_other(self, mag_genome_study: dict[str, Any]) -> None:
        root = build_submission_xml([mag_genome_study])
        tags = [el.text for el in root.findall(".//PROJECT_ATTRIBUTE/TAG") if el.text]
        values = [el.text for el in root.findall(".//PROJECT_ATTRIBUTE/VALUE") if el.text]
        assert "existing_study_type" in tags
        assert "new_study_type" in tags
        assert "Other" in values
        assert "Genome Sequencing" in values

    def test_no_project_attributes_when_no_study_type(self) -> None:
        study = {"alias": "no-type", "STUDY_TITLE": "No Type Study"}
        root = build_submission_xml([study])
        assert root.find(".//PROJECT_ATTRIBUTES") is None

    def test_hold_until_present_in_submission(self, basic_study: dict[str, Any]) -> None:
        root = build_submission_xml([basic_study], hold_until="2028-06-15")
        hold_el = root.find(".//HOLD")
        assert hold_el is not None
        assert hold_el.get("HoldUntilDate") == "2028-06-15"

    def test_hold_until_absent_when_not_provided(self, basic_study: dict[str, Any]) -> None:
        root = build_submission_xml([basic_study])
        assert root.find(".//HOLD") is None

    def test_modify_action_produces_modify_element(self, basic_study: dict[str, Any]) -> None:
        xml_str = self._to_str(build_submission_xml([basic_study], action="MODIFY"))
        assert "<MODIFY" in xml_str or "<MODIFY/>" in xml_str

    def test_add_action_produces_add_element(self, basic_study: dict[str, Any]) -> None:
        xml_str = self._to_str(build_submission_xml([basic_study]))
        assert "<ADD" in xml_str or "<ADD/>" in xml_str

    def test_modify_action_does_not_produce_add(self, basic_study: dict[str, Any]) -> None:
        xml_str = self._to_str(build_submission_xml([basic_study], action="MODIFY"))
        assert "<ADD" not in xml_str and "<ADD/>" not in xml_str

    def test_multiple_studies_produce_multiple_project_elements(
        self, basic_study: dict[str, Any], metagenomics_assembly_study: dict[str, Any]
    ) -> None:
        root = build_submission_xml([basic_study, metagenomics_assembly_study])
        assert len(root.findall(".//PROJECT")) == 2

    def test_alias_derived_from_title_when_absent(self) -> None:
        study = {"STUDY_TITLE": "My Derived Title"}
        root = build_submission_xml([study])
        project_el = root.find(".//PROJECT")
        assert project_el is not None
        alias = project_el.get("alias", "")
        assert "_" in alias or alias == "My_Derived_Title"[:50]

    def test_mag_genome_study_has_both_project_attributes(self, mag_genome_study: dict[str, Any]) -> None:
        root = build_submission_xml([mag_genome_study])
        attr_els = root.findall(".//PROJECT_ATTRIBUTE")
        assert len(attr_els) == 2
        pairs = {
            (attr_el.find("TAG").text or ""): (attr_el.find("VALUE").text or "")
            for attr_el in attr_els
            if attr_el.find("TAG") is not None and attr_el.find("VALUE") is not None
        }
        assert pairs.get("existing_study_type") == "Other"
        assert pairs.get("new_study_type") == "Genome Sequencing"


# ---------------------------------------------------------------------------
# B. Unit tests for build_manifest
# ---------------------------------------------------------------------------


class TestBuildManifest:

    def test_returns_bytes(self, basic_study: dict[str, Any]) -> None:
        result = build_manifest([basic_study])
        assert isinstance(result, bytes)

    def test_hold_until_passed_through(self, basic_study: dict[str, Any]) -> None:
        xml_bytes = build_manifest([basic_study], hold_until="2028-06-15")
        tree = ET.fromstring(xml_bytes)
        hold_el = tree.find(".//HOLD")
        assert hold_el is not None
        assert hold_el.get("HoldUntilDate") == "2028-06-15"

    def test_no_hold_when_not_provided(self, basic_study: dict[str, Any]) -> None:
        xml_bytes = build_manifest([basic_study])
        tree = ET.fromstring(xml_bytes)
        assert tree.find(".//HOLD") is None

    def test_modify_action_passed_through(self, basic_study: dict[str, Any]) -> None:
        xml_bytes = build_manifest([basic_study], action="MODIFY")
        xml_str = xml_bytes.decode("utf-8")
        assert "<MODIFY" in xml_str or "<MODIFY/>" in xml_str

    def test_add_action_is_default(self, basic_study: dict[str, Any]) -> None:
        xml_bytes = build_manifest([basic_study])
        xml_str = xml_bytes.decode("utf-8")
        assert "<ADD" in xml_str or "<ADD/>" in xml_str


# ---------------------------------------------------------------------------
# C. Unit tests for validate_manifest
# ---------------------------------------------------------------------------


def _valid_study_xml_bytes(alias: str = "study-1", title: str = "Test Study") -> bytes:
    xml_str = dedent(f"""\
        <?xml version='1.0' encoding='UTF-8'?>
        <WEBIN>
          <PROJECT_SET>
            <PROJECT alias="{alias}">
              <TITLE>{title}</TITLE>
              <SUBMISSION_PROJECT>
                <SEQUENCING_PROJECT/>
              </SUBMISSION_PROJECT>
            </PROJECT>
          </PROJECT_SET>
        </WEBIN>
    """)
    return xml_str.encode("utf-8")


class TestValidateManifest:

    def test_valid_xml_passes(self, real_xsd_dir: Path) -> None:
        is_valid, messages = validate_manifest(_valid_study_xml_bytes(), real_xsd_dir)
        assert is_valid, f"Expected valid; messages: {messages}"

    def test_missing_project_set_fails(self, real_xsd_dir: Path) -> None:
        xml_bytes = b"<?xml version='1.0'?><WEBIN/>"
        is_valid, messages = validate_manifest(xml_bytes, real_xsd_dir)
        assert not is_valid

    def test_missing_title_fails(self, real_xsd_dir: Path) -> None:
        xml_str = dedent("""\
            <?xml version='1.0' encoding='UTF-8'?>
            <WEBIN>
              <PROJECT_SET>
                <PROJECT alias="no-title">
                  <SUBMISSION_PROJECT><SEQUENCING_PROJECT/></SUBMISSION_PROJECT>
                </PROJECT>
              </PROJECT_SET>
            </WEBIN>
        """)
        is_valid, messages = validate_manifest(xml_str.encode(), real_xsd_dir)
        assert not is_valid

    def test_malformed_xml_fails_with_fallback(self, tmp_path: Path) -> None:
        bad_xml = b"<WEBIN><PROJECT_SET><PROJECT alias='x'><TITLE>Unclosed"
        is_valid, _ = validate_manifest(bad_xml, tmp_path)
        assert not is_valid

    def test_returns_tuple_of_bool_and_list(self, real_xsd_dir: Path) -> None:
        result = validate_manifest(_valid_study_xml_bytes(), real_xsd_dir)
        assert isinstance(result, tuple) and len(result) == 2
        is_valid, messages = result
        assert isinstance(is_valid, bool)
        assert isinstance(messages, list)

    def test_missing_submission_project_fails(self, real_xsd_dir: Path) -> None:
        xml_str = dedent("""\
            <?xml version='1.0' encoding='UTF-8'?>
            <WEBIN>
              <PROJECT_SET>
                <PROJECT alias="no-sp">
                  <TITLE>Some Title</TITLE>
                </PROJECT>
              </PROJECT_SET>
            </WEBIN>
        """)
        is_valid, messages = validate_manifest(xml_str.encode(), real_xsd_dir)
        assert not is_valid
        assert any("SUBMISSION_PROJECT" in m for m in messages)

    def test_empty_project_set_fails_with_fallback(self, tmp_path: Path) -> None:
        xml_bytes = b"<?xml version='1.0'?><WEBIN><PROJECT_SET/></WEBIN>"
        is_valid, _ = validate_manifest(xml_bytes, tmp_path)
        assert not is_valid


# ---------------------------------------------------------------------------
# D. Unit tests for parse_xml_receipt
# ---------------------------------------------------------------------------


class TestParseXmlReceipt:

    @staticmethod
    def _parse(xml_str: str) -> tuple[bool, list[dict[str, str]], list[str]]:
        return parse_xml_receipt(ET.fromstring(xml_str))

    def test_successful_project_receipt_returns_true(self) -> None:
        xml_str = dedent("""\
            <RECEIPT success="true" receiptDate="2024-01-15T12:00:00.000Z">
              <PROJECT accession="PRJEB12345" alias="my-study"
                       status="PRIVATE" holdUntilDate="2025-01-15">
                <EXT_ID accession="ERP012345" type="study"/>
              </PROJECT>
            </RECEIPT>
        """)
        success, accessions, _ = self._parse(xml_str)
        assert success is True

    def test_accession_fields_round_trip(self) -> None:
        xml_str = dedent("""\
            <RECEIPT success="true">
              <PROJECT accession="PRJEB12345" alias="my-study"
                       status="PRIVATE" holdUntilDate="2025-01-15">
                <EXT_ID accession="ERP012345" type="study"/>
              </PROJECT>
            </RECEIPT>
        """)
        _, accessions, _ = self._parse(xml_str)
        assert len(accessions) == 1
        acc = accessions[0]
        assert acc["accession"] == "PRJEB12345"
        assert acc["alias"] == "my-study"
        assert acc["status"] == "PRIVATE"
        assert acc["holdUntilDate"] == "2025-01-15"
        assert acc["external_accession"] == "ERP012345"
        assert acc["external_type"] == "study"

    def test_failed_receipt_returns_false(self) -> None:
        xml_str = dedent("""\
            <RECEIPT success="false">
              <MESSAGES>
                <ERROR>Center name "Unknown" is not permitted.</ERROR>
              </MESSAGES>
            </RECEIPT>
        """)
        success, _, _ = self._parse(xml_str)
        assert success is False

    def test_error_text_captured(self) -> None:
        xml_str = dedent("""\
            <RECEIPT success="false">
              <MESSAGES>
                <ERROR>Submission failed due to duplicate alias.</ERROR>
              </MESSAGES>
            </RECEIPT>
        """)
        _, _, messages = self._parse(xml_str)
        assert any("Submission failed due to duplicate alias" in m for m in messages)

    def test_study_tag_receipt_extracts_accession_and_alias(self) -> None:
        xml_str = dedent("""\
            <RECEIPT success="true">
              <STUDY accession="ERP099999" alias="study-alias-1" status="PRIVATE"/>
            </RECEIPT>
        """)
        success, accessions, _ = self._parse(xml_str)
        assert success is True
        assert len(accessions) == 1
        assert accessions[0]["accession"] == "ERP099999"
        assert accessions[0]["alias"] == "study-alias-1"

    def test_info_messages_captured(self) -> None:
        xml_str = dedent("""\
            <RECEIPT success="true">
              <PROJECT accession="PRJEB00001" alias="x" status="PRIVATE"/>
              <MESSAGES>
                <INFO>Submission processed successfully.</INFO>
              </MESSAGES>
            </RECEIPT>
        """)
        _, _, messages = self._parse(xml_str)
        assert any("Submission processed successfully" in m for m in messages)
        assert any(m.startswith("INFO:") for m in messages)

    def test_multiple_errors_all_captured(self) -> None:
        xml_str = dedent("""\
            <RECEIPT success="false">
              <MESSAGES>
                <ERROR>First error.</ERROR>
                <ERROR>Second error.</ERROR>
              </MESSAGES>
            </RECEIPT>
        """)
        _, _, messages = self._parse(xml_str)
        assert len([m for m in messages if m.startswith("ERROR:")]) == 2

    def test_no_messages_element_returns_empty_list(self) -> None:
        xml_str = '<RECEIPT success="true"><PROJECT accession="PRJEB1" alias="x" status="PRIVATE"/></RECEIPT>'
        _, _, messages = self._parse(xml_str)
        assert messages == []

    def test_missing_success_defaults_to_false(self) -> None:
        success, _, _ = self._parse("<RECEIPT/>")
        assert success is False


# ---------------------------------------------------------------------------
# E. Unit tests for find_duplicate_studies and fetch_account_studies
# ---------------------------------------------------------------------------


class TestFindDuplicateStudies:

    @staticmethod
    def _account(title: str = "", alias: str = "", accession: str = "PRJEB00001") -> dict[str, str]:
        return {"title": title, "alias": alias, "accession": accession, "secondary_accession": "", "status": "PRIVATE"}

    def test_exact_alias_match_detected(self) -> None:
        dups = find_duplicate_studies(
            [{"STUDY_TITLE": "Different", "alias": "my-alias-x"}],
            [self._account(title="Other", alias="my-alias-x", accession="PRJEB10")],
        )
        assert 0 in dups
        assert dups[0]["accession"] == "PRJEB10"
        assert "alias" in dups[0]["match_reason"]

    def test_exact_title_match_detected(self) -> None:
        dups = find_duplicate_studies(
            [{"STUDY_TITLE": "My Metagenomics Study"}],
            [self._account(title="My Metagenomics Study", accession="PRJEB20")],
        )
        assert 0 in dups
        assert "title" in dups[0]["match_reason"]

    def test_no_match_returns_empty_dict(self) -> None:
        dups = find_duplicate_studies(
            [{"STUDY_TITLE": "Novel Study", "alias": "novel"}],
            [self._account(title="Existing Study", alias="existing")],
        )
        assert dups == {}

    def test_empty_account_returns_empty_dict(self) -> None:
        assert find_duplicate_studies([{"STUDY_TITLE": "Any"}], []) == {}

    def test_empty_new_studies_returns_empty_dict(self) -> None:
        assert find_duplicate_studies([], [self._account(title="Existing")]) == {}

    def test_partial_title_not_a_duplicate(self) -> None:
        dups = find_duplicate_studies(
            [{"STUDY_TITLE": "Metagenomics"}],
            [self._account(title="Metagenomics Assembly Study")],
        )
        assert dups == {}

    def test_only_matching_index_flagged(self) -> None:
        dups = find_duplicate_studies(
            [{"STUDY_TITLE": "Old Study"}, {"STUDY_TITLE": "New Study"}],
            [self._account(title="Old Study", alias="old-alias", accession="PRJEB50")],
        )
        assert 0 in dups
        assert 1 not in dups

    def test_index_corresponds_to_position_in_list(self) -> None:
        dups = find_duplicate_studies(
            [{"STUDY_TITLE": "Study A"}, {"STUDY_TITLE": "Study B"}, {"STUDY_TITLE": "Study C"}],
            [self._account(title="Study C", accession="PRJEB33")],
        )
        assert 2 in dups
        assert dups[2]["accession"] == "PRJEB33"


class TestNormalizeStudyReport:

    def test_title_direct(self) -> None:
        assert _normalize_study_report({"title": "My Title", "accession": "PRJEB1"})["title"] == "My Title"

    def test_title_study_title_fallback(self) -> None:
        assert _normalize_study_report({"studyTitle": "Fallback"})["title"] == "Fallback"

    def test_alias_direct(self) -> None:
        assert _normalize_study_report({"alias": "direct-alias"})["alias"] == "direct-alias"

    def test_alias_study_alias_fallback(self) -> None:
        assert _normalize_study_report({"studyAlias": "study-alias-fallback"})["alias"] == "study-alias-fallback"

    def test_accession_direct(self) -> None:
        assert _normalize_study_report({"accession": "PRJEB5"})["accession"] == "PRJEB5"

    def test_accession_study_accession_fallback(self) -> None:
        result = _normalize_study_report({"title": "T", "studyAccession": "PRJEB99", "accession": ""})
        assert result["accession"] == "PRJEB99"

    def test_missing_fields_default_to_empty_string(self) -> None:
        result = _normalize_study_report({})
        assert result["title"] == ""
        assert result["alias"] == ""
        assert result["accession"] == ""

    def test_status_defaults_to_unknown(self) -> None:
        assert _normalize_study_report({})["status"] == "UNKNOWN"

    def test_release_status_mapped_to_status(self) -> None:
        assert _normalize_study_report({"releaseStatus": "PUBLIC"})["status"] == "PUBLIC"


class TestFetchAccountStudies:

    def test_calls_fetch_account_records_with_correct_urls(self, auth: HTTPBasicAuth) -> None:
        with patch("submit_study.common.fetch_account_records", return_value=[]) as mock_fetch:
            fetch_account_studies(auth, use_test=False)
            assert mock_fetch.call_args.kwargs.get("prod_url") == _PROD_REPORTS_URL
            assert mock_fetch.call_args.kwargs.get("test_url") == _TEST_REPORTS_URL

    def test_passes_callable_normalizer(self, auth: HTTPBasicAuth) -> None:
        with patch("submit_study.common.fetch_account_records", return_value=[]) as mock_fetch:
            fetch_account_studies(auth, use_test=False)
            normalizer = mock_fetch.call_args.kwargs.get("normalizer")
            assert callable(normalizer)

    def test_normalizer_handles_title_variants(self, auth: HTTPBasicAuth) -> None:
        captured: list[Any] = []

        def capture(*args: Any, **kwargs: Any) -> list:
            captured.append(kwargs.get("normalizer"))
            return []

        with patch("submit_study.common.fetch_account_records", side_effect=capture):
            fetch_account_studies(auth, use_test=False)

        norm = captured[0]
        assert norm({"title": "Direct Title"})["title"] == "Direct Title"
        assert norm({"studyTitle": "Fallback Title"})["title"] == "Fallback Title"


# ---------------------------------------------------------------------------
# F. CLI integration tests for main()
# ---------------------------------------------------------------------------


def _extract_json(output: str) -> dict[str, Any]:
    """Extract the last JSON object from mixed CLI output."""
    depth, end, start = 0, -1, -1
    for i in range(len(output) - 1, -1, -1):
        ch = output[i]
        if ch == "}":
            if depth == 0:
                end = i
            depth += 1
        elif ch == "{":
            depth -= 1
            if depth == 0:
                start = i
                break
    if start == -1 or end == -1:
        raise ValueError(f"No JSON object found in output: {output[:200]!r}")
    return json.loads(output[start:end + 1])


def _make_study_json(study: dict[str, Any]) -> str:
    return json.dumps({"studies": [study]})


@pytest.fixture
def minimal_study() -> dict[str, Any]:
    return {
        "alias": "cli-metagenomics-001",
        "STUDY_TITLE": "CLI Metagenomics Test Study",
        "STUDY_ABSTRACT": "Abstract for CLI test.",
        "existing_study_type": "Metagenomics",
    }


class TestMainCli:
    _CRED_TARGET = "submit_study.common.get_credentials"
    _SUBMIT_TARGET = "submit_study.common.submit_xml"
    _LINKML_TARGET = "submit_study.common.validate_against_linkml"

    def _invoke(self, runner: CliRunner, args: list[str], input_filename: str, input_content: str) -> Any:
        with runner.isolated_filesystem():
            Path(input_filename).write_text(input_content)
            Path("schema.yaml").write_text(_MINIMAL_SCHEMA_YAML)
            base_args = ["--linkml", "schema.yaml", "--xsd", _REAL_XSD_DIR]
            result = runner.invoke(
                app,
                ["--input", input_filename] + base_args + args,
                catch_exceptions=False,
            )
        return result

    def test_json_automated_dry_run_exits_0(self, runner: CliRunner, minimal_study: dict[str, Any]) -> None:
        content = _make_study_json(minimal_study)
        with patch(self._CRED_TARGET, return_value=("Webin-12345", "pass")), \
             patch(self._LINKML_TARGET, return_value=(True, [])):
            result = self._invoke(runner, ["--automated", "--dry-run"], "studies.json", content)
        assert result.exit_code == 0, f"output: {result.output}"
        assert "submitted" in _extract_json(result.output)

    def test_duplicate_detected_without_force_skips_submission(
        self, runner: CliRunner, minimal_study: dict[str, Any]
    ) -> None:
        existing = {
            "title": minimal_study["STUDY_TITLE"],
            "alias": minimal_study["alias"],
            "accession": "PRJEB55555",
            "secondary_accession": "ERP055555",
            "status": "PRIVATE",
        }
        content = _make_study_json(minimal_study)
        with runner.isolated_filesystem():
            Path("studies.json").write_text(content)
            Path("schema.yaml").write_text(_MINIMAL_SCHEMA_YAML)
            base_args = ["--linkml", "schema.yaml", "--xsd", _REAL_XSD_DIR]
            with patch(self._CRED_TARGET, return_value=("Webin-12345", "pass")), \
                 patch("submit_study.fetch_account_studies", return_value=[existing]):
                result = runner.invoke(
                    app,
                    ["--input", "studies.json"] + base_args,
                    catch_exceptions=False,
                )
        assert result.exit_code == 0, f"output: {result.output}"
        data = _extract_json(result.output)
        assert len(data["duplicates"]) == 1
        assert data["duplicates"][0]["existing_accession"] == "PRJEB55555"
        assert data["submitted"] == []

    def test_force_flag_with_duplicate_triggers_modify(
        self, runner: CliRunner, minimal_study: dict[str, Any]
    ) -> None:
        existing = {
            "title": minimal_study["STUDY_TITLE"],
            "alias": minimal_study["alias"],
            "accession": "PRJEB66666",
            "secondary_accession": "ERP066666",
            "status": "PRIVATE",
        }
        receipt_xml = ET.fromstring(
            '<RECEIPT success="true">'
            '<PROJECT accession="PRJEB66666" alias="cli-metagenomics-001" status="PRIVATE"/>'
            "</RECEIPT>"
        )
        content = _make_study_json(minimal_study)
        with runner.isolated_filesystem():
            Path("studies.json").write_text(content)
            Path("schema.yaml").write_text(_MINIMAL_SCHEMA_YAML)
            base_args = ["--linkml", "schema.yaml", "--xsd", _REAL_XSD_DIR]
            with patch(self._CRED_TARGET, return_value=("Webin-12345", "pass")), \
                 patch("submit_study.fetch_account_studies", return_value=[existing]), \
                 patch(self._SUBMIT_TARGET, return_value=receipt_xml), \
                 patch(self._LINKML_TARGET, return_value=(True, [])):
                result = runner.invoke(
                    app,
                    ["--input", "studies.json", "--force"] + base_args,
                    catch_exceptions=False,
                )
        assert result.exit_code == 0, f"output: {result.output}"
        data = _extract_json(result.output)
        assert len(data["modified"]) == 1
        assert data["modified"][0]["accession"] == "PRJEB66666"

    def test_failed_submission_exits_1(self, runner: CliRunner, minimal_study: dict[str, Any]) -> None:
        import requests
        content = _make_study_json(minimal_study)
        http_error = requests.exceptions.HTTPError(response=MagicMock(status_code=500, text="err"))
        with patch(self._CRED_TARGET, return_value=("Webin-12345", "pass")), \
             patch(self._SUBMIT_TARGET, side_effect=http_error), \
             patch(self._LINKML_TARGET, return_value=(True, [])):
            result = self._invoke(runner, ["--automated"], "studies.json", content)
        assert result.exit_code == 1

    def test_test_flag_routes_to_test_url(self, runner: CliRunner, minimal_study: dict[str, Any]) -> None:
        receipt_xml = ET.fromstring(
            '<RECEIPT success="true">'
            '<PROJECT accession="PRJEB00001" alias="cli-metagenomics-001" status="PRIVATE"/>'
            "</RECEIPT>"
        )
        content = _make_study_json(minimal_study)
        with runner.isolated_filesystem():
            Path("studies.json").write_text(content)
            Path("schema.yaml").write_text(_MINIMAL_SCHEMA_YAML)
            base_args = ["--linkml", "schema.yaml", "--xsd", _REAL_XSD_DIR]
            with patch(self._CRED_TARGET, return_value=("Webin-12345", "pass")), \
                 patch(self._SUBMIT_TARGET, return_value=receipt_xml) as mock_submit, \
                 patch(self._LINKML_TARGET, return_value=(True, [])):
                result = runner.invoke(
                    app,
                    ["--input", "studies.json", "--automated", "--test"] + base_args,
                    catch_exceptions=False,
                )
        assert result.exit_code == 0, f"output: {result.output}"
        called_url = mock_submit.call_args[0][0]
        assert "wwwdev" in called_url, f"Expected test URL; got {called_url}"

    def test_no_test_flag_routes_to_production_url(self, runner: CliRunner, minimal_study: dict[str, Any]) -> None:
        receipt_xml = ET.fromstring(
            '<RECEIPT success="true">'
            '<PROJECT accession="PRJEB00002" alias="cli-metagenomics-001" status="PRIVATE"/>'
            "</RECEIPT>"
        )
        content = _make_study_json(minimal_study)
        with runner.isolated_filesystem():
            Path("studies.json").write_text(content)
            Path("schema.yaml").write_text(_MINIMAL_SCHEMA_YAML)
            base_args = ["--linkml", "schema.yaml", "--xsd", _REAL_XSD_DIR]
            with patch(self._CRED_TARGET, return_value=("Webin-12345", "pass")), \
                 patch(self._SUBMIT_TARGET, return_value=receipt_xml) as mock_submit, \
                 patch(self._LINKML_TARGET, return_value=(True, [])):
                result = runner.invoke(
                    app,
                    ["--input", "studies.json", "--automated"] + base_args,
                    catch_exceptions=False,
                )
        assert result.exit_code == 0, f"output: {result.output}"
        called_url = mock_submit.call_args[0][0]
        assert "wwwdev" not in called_url, f"Expected prod URL; got {called_url}"

    def test_output_flag_writes_results_to_file(self, runner: CliRunner, minimal_study: dict[str, Any]) -> None:
        content = _make_study_json(minimal_study)
        with runner.isolated_filesystem():
            Path("studies.json").write_text(content)
            Path("schema.yaml").write_text(_MINIMAL_SCHEMA_YAML)
            base_args = ["--linkml", "schema.yaml", "--xsd", _REAL_XSD_DIR]
            with patch(self._CRED_TARGET, return_value=("Webin-12345", "pass")), \
                 patch(self._LINKML_TARGET, return_value=(True, [])):
                result = runner.invoke(
                    app,
                    ["--input", "studies.json", "--automated", "--dry-run", "--output", "results.json"] + base_args,
                    catch_exceptions=False,
                )
            assert result.exit_code == 0, f"output: {result.output}"
            assert Path("results.json").exists()
            data = json.loads(Path("results.json").read_text())
            assert "submitted" in data


# ---------------------------------------------------------------------------
# Parametrized tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "study_type,new_type,expect_new_type",
    [
        ("Metagenomics", None, False),
        ("RNASeq", None, False),
        ("Other", "Genome Sequencing", True),
        ("Other", "Transcriptome Analysis", True),
        ("Other", None, False),
    ],
)
def test_project_attribute_new_study_type_conditional(
    study_type: str, new_type: str | None, expect_new_type: bool
) -> None:
    study: dict[str, Any] = {
        "alias": "param-test",
        "STUDY_TITLE": "Parametrized Study",
        "existing_study_type": study_type,
    }
    if new_type is not None:
        study["new_study_type"] = new_type
    root = build_submission_xml([study])
    tags = [el.text for el in root.findall(".//PROJECT_ATTRIBUTE/TAG") if el.text]
    if expect_new_type:
        assert "new_study_type" in tags
    else:
        assert "new_study_type" not in tags


@pytest.mark.parametrize(
    "hold_until,expect_hold",
    [("2027-03-01", True), ("2028-12-31", True), (None, False)],
)
def test_hold_until_element_conditional(hold_until: str | None, expect_hold: bool) -> None:
    study = {"alias": "hold-test", "STUDY_TITLE": "Hold Date Test"}
    root = build_submission_xml([study], hold_until=hold_until)
    hold_el = root.find(".//HOLD")
    if expect_hold:
        assert hold_el is not None
        assert hold_el.get("HoldUntilDate") == hold_until
    else:
        assert hold_el is None


@pytest.mark.parametrize("action", ["ADD", "MODIFY"])
def test_submission_action_element_present(action: str) -> None:
    study = {"alias": "action-test", "STUDY_TITLE": "Action Test"}
    root = build_submission_xml([study], action=action)
    xml_str = ET.tostring(root, encoding="unicode")
    assert f"<{action}" in xml_str or f"<{action}/>" in xml_str
    opposite = "MODIFY" if action == "ADD" else "ADD"
    assert f"<{opposite}" not in xml_str
