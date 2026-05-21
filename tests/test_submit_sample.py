#!/usr/bin/env python3
"""Tests for submit_sample.py — ENA sample submission pipeline.

Covers:
    A. Unit tests for build_manifest / build_submission_xml / _add_sample_element
    B. Unit tests for validate_manifest
    C. Unit tests for parse_xml_receipt / submit_manifest
    D. Unit tests for find_duplicate_samples / _normalize_sample_report / fetch_account_samples
    E. CLI integration tests for main() using typer.testing.CliRunner

Usage:
    pytest test_submit_sample.py -v
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path
from textwrap import dedent
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from requests.auth import HTTPBasicAuth
from typer.testing import CliRunner

import sys
sys.path.insert(0, str(Path(__file__).parent / "scripts"))

from submit_sample import (  # noqa: E402
    _normalize_sample_report,
    build_manifest,
    build_submission_xml,
    fetch_account_samples,
    find_duplicate_samples,
    main,
    parse_xml_receipt,
    submit_manifest,
    validate_manifest,
    app,
)

_PROD_REPORTS_URL = "https://www.ebi.ac.uk/ena/submit/report/samples"
_TEST_REPORTS_URL = "https://wwwdev.ebi.ac.uk/ena/submit/report/samples"


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def basic_sample() -> dict[str, Any]:
    return {
        "alias": "test-sample-001",
        "SAMPLE_TITLE": "A Basic Test Sample",
        "TAXON_ID": 1235509,
        "SCIENTIFIC_NAME": "synthetic metagenome",
        "collection_date": "2024-06-01",
    }


@pytest.fixture
def minimal_sample() -> dict[str, Any]:
    return {"alias": "minimal-001", "TAXON_ID": 9606, "SAMPLE_TITLE": "Minimal Sample"}


@pytest.fixture
def minimal_schema() -> dict[str, Any]:
    return {"name": "TestSchema", "classes": {}, "slots": {}}


@pytest.fixture
def erc_schema() -> dict[str, Any]:
    return {"name": "ERC000025", "classes": {}, "slots": {}}


@pytest.fixture
def auth() -> HTTPBasicAuth:
    return HTTPBasicAuth("Webin-12345", "pass")


@pytest.fixture
def account_sample_record() -> dict[str, str]:
    return {
        "title": "Existing Sample Title",
        "alias": "existing-sample-alias",
        "accession": "ERS099001",
        "secondary_accession": "SAMEA099001",
        "status": "PRIVATE",
    }


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# ---------------------------------------------------------------------------
# A. build_manifest / build_submission_xml
# ---------------------------------------------------------------------------

class TestBuildSubmissionXml:
    """Unit tests for the low-level build_submission_xml / _add_sample_element functions."""

    @staticmethod
    def _str(root: ET.Element) -> str:
        return ET.tostring(root, encoding="unicode")

    def test_sample_title_round_trips(self, basic_sample: dict[str, Any]) -> None:
        root = build_submission_xml([basic_sample])
        assert root.find(".//TITLE").text == basic_sample["SAMPLE_TITLE"]

    def test_taxon_id_round_trips(self, basic_sample: dict[str, Any]) -> None:
        root = build_submission_xml([basic_sample])
        assert root.find(".//TAXON_ID").text == str(basic_sample["TAXON_ID"])

    def test_alias_round_trips(self, basic_sample: dict[str, Any]) -> None:
        root = build_submission_xml([basic_sample])
        assert root.find(".//SAMPLE").get("alias") == basic_sample["alias"]

    def test_scientific_name_present_when_given(self, basic_sample: dict[str, Any]) -> None:
        root = build_submission_xml([basic_sample])
        el = root.find(".//SCIENTIFIC_NAME")
        assert el is not None
        assert el.text == basic_sample["SCIENTIFIC_NAME"]

    def test_common_name_omitted_when_absent(self, basic_sample: dict[str, Any]) -> None:
        root = build_submission_xml([basic_sample])
        assert root.find(".//COMMON_NAME") is None

    def test_common_name_present_when_given(self) -> None:
        sample = {"alias": "s1", "TAXON_ID": 9606, "COMMON_NAME": "human"}
        root = build_submission_xml([sample])
        assert root.find(".//COMMON_NAME").text == "human"

    def test_non_reserved_fields_become_sample_attributes(self, basic_sample: dict[str, Any]) -> None:
        root = build_submission_xml([basic_sample])
        tags = [el.text for el in root.findall(".//SAMPLE_ATTRIBUTE/TAG")]
        assert "collection_date" in tags

    def test_checklist_id_is_first_sample_attribute(self, basic_sample: dict[str, Any]) -> None:
        root = build_submission_xml([basic_sample], checklist_id="ERC000025")
        first_tag = root.find(".//SAMPLE_ATTRIBUTES/SAMPLE_ATTRIBUTE/TAG")
        assert first_tag is not None
        assert first_tag.text == "ENA-CHECKLIST"

    def test_units_added_when_slot_to_unit_provided(self, basic_sample: dict[str, Any]) -> None:
        root = build_submission_xml([basic_sample], slot_to_unit={"collection_date": "ISO8601"})
        units_els = root.findall(".//SAMPLE_ATTRIBUTE/UNITS")
        assert any(el.text == "ISO8601" for el in units_els)

    def test_slot_to_title_renames_tag(self, basic_sample: dict[str, Any]) -> None:
        root = build_submission_xml([basic_sample], slot_to_title={"collection_date": "Collection Date"})
        tags = [el.text for el in root.findall(".//SAMPLE_ATTRIBUTE/TAG")]
        assert "Collection Date" in tags
        assert "collection_date" not in tags

    def test_hold_until_produces_hold_element(self, basic_sample: dict[str, Any]) -> None:
        root = build_submission_xml([basic_sample], hold_until="2028-01-01")
        hold_el = root.find(".//HOLD")
        assert hold_el is not None
        assert hold_el.get("HoldUntilDate") == "2028-01-01"

    def test_no_hold_element_when_hold_until_absent(self, basic_sample: dict[str, Any]) -> None:
        root = build_submission_xml([basic_sample])
        assert root.find(".//HOLD") is None

    def test_modify_action_produces_modify_element(self, basic_sample: dict[str, Any]) -> None:
        root = build_submission_xml([basic_sample], action="MODIFY")
        assert "<MODIFY" in self._str(root) or "<MODIFY/>" in self._str(root)
        assert "<ADD" not in self._str(root)

    def test_add_action_produces_add_element(self, basic_sample: dict[str, Any]) -> None:
        root = build_submission_xml([basic_sample])
        xml_str = self._str(root)
        assert "<ADD" in xml_str or "<ADD/>" in xml_str

    def test_multiple_samples_produce_multiple_sample_elements(
        self, basic_sample: dict[str, Any], minimal_sample: dict[str, Any]
    ) -> None:
        root = build_submission_xml([basic_sample, minimal_sample])
        assert len(root.findall(".//SAMPLE")) == 2

    def test_alias_derived_from_title_when_absent(self) -> None:
        sample = {"SAMPLE_TITLE": "My Derived Sample", "TAXON_ID": 9606}
        root = build_submission_xml([sample])
        alias = root.find(".//SAMPLE").get("alias", "")
        assert "My_Derived_Sample" in alias or "_" in alias

    def test_reserved_fields_not_in_sample_attributes(self, basic_sample: dict[str, Any]) -> None:
        root = build_submission_xml([basic_sample])
        tags = {el.text for el in root.findall(".//SAMPLE_ATTRIBUTE/TAG")}
        for reserved in ("alias", "SAMPLE_TITLE", "TAXON_ID", "SCIENTIFIC_NAME"):
            assert reserved not in tags

    def test_sample_name_element_always_present(self, basic_sample: dict[str, Any]) -> None:
        root = build_submission_xml([basic_sample])
        assert root.find(".//SAMPLE_NAME") is not None


class TestBuildManifest:
    """Integration tests for build_manifest() — schema-aware XML builder."""

    def test_returns_bytes(self, basic_sample: dict[str, Any], minimal_schema: dict[str, Any], tmp_path: Path) -> None:
        with patch("submit_sample.common.build_slot_to_title_map", return_value={}):
            result = build_manifest([basic_sample], minimal_schema, tmp_path)
        assert isinstance(result, bytes)
        assert b"SAMPLE_SET" in result

    def test_erc_schema_name_sets_checklist_id(
        self, basic_sample: dict[str, Any], erc_schema: dict[str, Any], tmp_path: Path
    ) -> None:
        with patch("submit_sample.common.build_slot_to_title_map", return_value={}):
            xml_bytes = build_manifest([basic_sample], erc_schema, tmp_path)
        root = ET.fromstring(xml_bytes)
        tags = [el.text for el in root.findall(".//SAMPLE_ATTRIBUTE/TAG")]
        assert "ENA-CHECKLIST" in tags

    def test_non_erc_schema_name_omits_checklist(
        self, basic_sample: dict[str, Any], minimal_schema: dict[str, Any], tmp_path: Path
    ) -> None:
        with patch("submit_sample.common.build_slot_to_title_map", return_value={}):
            xml_bytes = build_manifest([basic_sample], minimal_schema, tmp_path)
        root = ET.fromstring(xml_bytes)
        tags = [el.text for el in root.findall(".//SAMPLE_ATTRIBUTE/TAG")]
        assert "ENA-CHECKLIST" not in tags

    def test_hold_until_passed_through(
        self, basic_sample: dict[str, Any], minimal_schema: dict[str, Any], tmp_path: Path
    ) -> None:
        with patch("submit_sample.common.build_slot_to_title_map", return_value={}):
            xml_bytes = build_manifest([basic_sample], minimal_schema, tmp_path, hold_until="2028-06-15")
        root = ET.fromstring(xml_bytes)
        hold_el = root.find(".//HOLD")
        assert hold_el is not None
        assert hold_el.get("HoldUntilDate") == "2028-06-15"

    def test_modify_action_passed_through(
        self, basic_sample: dict[str, Any], minimal_schema: dict[str, Any], tmp_path: Path
    ) -> None:
        with patch("submit_sample.common.build_slot_to_title_map", return_value={}):
            xml_bytes = build_manifest([basic_sample], minimal_schema, tmp_path, action="MODIFY")
        assert b"MODIFY" in xml_bytes


# ---------------------------------------------------------------------------
# B. validate_manifest
# ---------------------------------------------------------------------------

@pytest.fixture
def real_xsd_dir() -> Path:
    """Return the real ENA schema directory containing SRA.sample.xsd."""
    return Path(__file__).parent / "assets" / "ena_schema"


class TestValidateManifest:
    """Unit tests for validate_manifest().

    Tests that require schema validation use the real assets/ena_schema directory.
    The fallback structural checker is exercised when no XSD is available (tmp_path).
    """

    @staticmethod
    def _valid_xml(alias: str = "sample-1", taxon: str = "9606") -> bytes:
        return dedent(f"""\
            <?xml version='1.0' encoding='UTF-8'?>
            <WEBIN>
              <SAMPLE_SET>
                <SAMPLE alias="{alias}">
                  <TITLE>Test Sample</TITLE>
                  <SAMPLE_NAME><TAXON_ID>{taxon}</TAXON_ID></SAMPLE_NAME>
                </SAMPLE>
              </SAMPLE_SET>
            </WEBIN>
        """).encode("utf-8")

    def test_valid_xml_passes(self, real_xsd_dir: Path) -> None:
        is_valid, messages = validate_manifest(self._valid_xml(), real_xsd_dir)
        assert is_valid, f"Expected valid; messages: {messages}"

    def test_missing_sample_set_fails(self, real_xsd_dir: Path) -> None:
        xml_bytes = b"<?xml version='1.0'?><WEBIN/>"
        is_valid, messages = validate_manifest(xml_bytes, real_xsd_dir)
        assert not is_valid

    def test_missing_taxon_id_fails(self, real_xsd_dir: Path) -> None:
        xml_bytes = dedent("""\
            <WEBIN>
              <SAMPLE_SET>
                <SAMPLE alias="no-taxon">
                  <SAMPLE_NAME></SAMPLE_NAME>
                </SAMPLE>
              </SAMPLE_SET>
            </WEBIN>
        """).encode("utf-8")
        is_valid, messages = validate_manifest(xml_bytes, real_xsd_dir)
        assert not is_valid

    def test_malformed_xml_fails_with_fallback(self, tmp_path: Path) -> None:
        """Malformed XML fails even without the XSD file (fallback structural check)."""
        is_valid, messages = validate_manifest(b"<WEBIN><SAMPLE_SET><SAMPLE unclosed", tmp_path)
        assert not is_valid

    def test_returns_tuple_of_bool_and_list(self, real_xsd_dir: Path) -> None:
        result = validate_manifest(self._valid_xml(), real_xsd_dir)
        assert isinstance(result, tuple) and len(result) == 2
        assert isinstance(result[0], bool)
        assert isinstance(result[1], list)

    def test_no_samples_in_set_fails_with_fallback(self, tmp_path: Path) -> None:
        """Empty SAMPLE_SET fails via the structural fallback checker."""
        xml_bytes = b"<WEBIN><SAMPLE_SET></SAMPLE_SET></WEBIN>"
        is_valid, _ = validate_manifest(xml_bytes, tmp_path)
        assert not is_valid


# ---------------------------------------------------------------------------
# C. parse_xml_receipt / submit_manifest
# ---------------------------------------------------------------------------

class TestParseXmlReceipt:
    """Unit tests for parse_xml_receipt()."""

    @staticmethod
    def _parse(xml_str: str) -> tuple[bool, list[dict[str, str]], list[str]]:
        return parse_xml_receipt(ET.fromstring(xml_str))

    def test_success_receipt_returns_true(self) -> None:
        xml_str = dedent("""\
            <RECEIPT success="true">
              <SAMPLE accession="ERS123456" alias="s1" status="PRIVATE" holdUntilDate="2026-01-01">
                <EXT_ID accession="SAMEA123456" type="biosample"/>
              </SAMPLE>
            </RECEIPT>
        """)
        success, accessions, messages = self._parse(xml_str)
        assert success is True

    def test_accession_fields_round_trip(self) -> None:
        xml_str = dedent("""\
            <RECEIPT success="true">
              <SAMPLE accession="ERS123456" alias="s1" status="PRIVATE" holdUntilDate="2026-01-01">
                <EXT_ID accession="SAMEA123456" type="biosample"/>
              </SAMPLE>
            </RECEIPT>
        """)
        _, accessions, _ = self._parse(xml_str)
        assert len(accessions) == 1
        acc = accessions[0]
        assert acc["accession"] == "ERS123456"
        assert acc["alias"] == "s1"
        assert acc["status"] == "PRIVATE"
        assert acc["holdUntilDate"] == "2026-01-01"
        assert acc["external_accession"] == "SAMEA123456"
        assert acc["external_type"] == "biosample"

    def test_failed_receipt_returns_false(self) -> None:
        xml_str = '<RECEIPT success="false"><MESSAGES><ERROR>Duplicate alias.</ERROR></MESSAGES></RECEIPT>'
        success, _, _ = self._parse(xml_str)
        assert success is False

    def test_error_text_captured(self) -> None:
        xml_str = '<RECEIPT success="false"><MESSAGES><ERROR>Alias already registered.</ERROR></MESSAGES></RECEIPT>'
        _, _, messages = self._parse(xml_str)
        assert any("Alias already registered" in m for m in messages)
        assert any(m.startswith("ERROR:") for m in messages)

    def test_info_messages_captured(self) -> None:
        xml_str = dedent("""\
            <RECEIPT success="true">
              <SAMPLE accession="ERS1" alias="x" status="PRIVATE"/>
              <MESSAGES><INFO>Submission processed.</INFO></MESSAGES>
            </RECEIPT>
        """)
        _, _, messages = self._parse(xml_str)
        assert any("Submission processed" in m for m in messages)
        assert any(m.startswith("INFO:") for m in messages)

    def test_multiple_samples_all_captured(self) -> None:
        xml_str = dedent("""\
            <RECEIPT success="true">
              <SAMPLE accession="ERS1" alias="a1" status="PRIVATE"/>
              <SAMPLE accession="ERS2" alias="a2" status="PRIVATE"/>
            </RECEIPT>
        """)
        _, accessions, _ = self._parse(xml_str)
        assert len(accessions) == 2

    def test_missing_success_defaults_to_false(self) -> None:
        success, _, _ = self._parse("<RECEIPT/>")
        assert success is False

    def test_no_messages_element_returns_empty_list(self) -> None:
        xml_str = '<RECEIPT success="true"><SAMPLE accession="ERS1" alias="x" status="PRIVATE"/></RECEIPT>'
        _, _, messages = self._parse(xml_str)
        assert messages == []

    def test_multiple_errors_all_captured(self) -> None:
        xml_str = dedent("""\
            <RECEIPT success="false">
              <MESSAGES>
                <ERROR>Error one.</ERROR>
                <ERROR>Error two.</ERROR>
              </MESSAGES>
            </RECEIPT>
        """)
        _, _, messages = self._parse(xml_str)
        error_msgs = [m for m in messages if m.startswith("ERROR:")]
        assert len(error_msgs) == 2


class TestSubmitManifest:
    """Unit tests for submit_manifest() — wraps common.submit_xml + parse_xml_receipt."""

    def test_successful_submission_returns_accessions(self, auth: HTTPBasicAuth) -> None:
        receipt_el = ET.fromstring(
            '<RECEIPT success="true">'
            '<SAMPLE accession="ERS999" alias="s1" status="PRIVATE"/>'
            "</RECEIPT>"
        )
        with patch("submit_sample.common.submit_xml", return_value=receipt_el):
            success, accessions, messages = submit_manifest(b"<xml/>", "https://example.com", auth)
        assert success is True
        assert accessions[0]["accession"] == "ERS999"

    def test_http_error_propagates(self, auth: HTTPBasicAuth) -> None:
        import requests as req
        err = req.exceptions.HTTPError(response=MagicMock(status_code=500, text="fail"))
        with patch("submit_sample.common.submit_xml", side_effect=err):
            with pytest.raises(req.exceptions.HTTPError):
                submit_manifest(b"<xml/>", "https://example.com", auth)


# ---------------------------------------------------------------------------
# D. find_duplicate_samples / _normalize_sample_report / fetch_account_samples
# ---------------------------------------------------------------------------

class TestFindDuplicateSamples:
    """Unit tests for find_duplicate_samples()."""

    @staticmethod
    def _account_record(title: str = "", alias: str = "", accession: str = "ERS001") -> dict[str, str]:
        return {"title": title, "alias": alias, "accession": accession, "secondary_accession": "", "status": "PRIVATE"}

    def test_exact_alias_match_detected(self) -> None:
        new = [{"SAMPLE_TITLE": "Different", "alias": "my-alias"}]
        account = [self._account_record(alias="my-alias", accession="ERS10")]
        dups = find_duplicate_samples(new, account)
        assert 0 in dups
        assert dups[0]["accession"] == "ERS10"
        assert "alias" in dups[0]["match_reason"]

    def test_exact_title_match_detected(self) -> None:
        new = [{"SAMPLE_TITLE": "My Metagenome Sample"}]
        account = [self._account_record(title="My Metagenome Sample", accession="ERS20")]
        dups = find_duplicate_samples(new, account)
        assert 0 in dups
        assert "title" in dups[0]["match_reason"]

    def test_no_match_returns_empty_dict(self) -> None:
        new = [{"SAMPLE_TITLE": "Novel Sample", "alias": "novel-alias"}]
        account = [self._account_record(title="Old Sample", alias="old-alias")]
        assert find_duplicate_samples(new, account) == {}

    def test_empty_account_returns_empty_dict(self) -> None:
        assert find_duplicate_samples([{"SAMPLE_TITLE": "Any"}], []) == {}

    def test_empty_new_samples_returns_empty_dict(self) -> None:
        assert find_duplicate_samples([], [self._account_record(title="Existing")]) == {}

    def test_partial_title_not_a_duplicate(self) -> None:
        new = [{"SAMPLE_TITLE": "Metagenome"}]
        account = [self._account_record(title="Metagenome Sample 1")]
        assert find_duplicate_samples(new, account) == {}

    def test_only_matching_index_flagged(self) -> None:
        account = [self._account_record(title="Old Sample", accession="ERS50")]
        new = [{"SAMPLE_TITLE": "Old Sample"}, {"SAMPLE_TITLE": "New Sample"}]
        dups = find_duplicate_samples(new, account)
        assert 0 in dups
        assert 1 not in dups

    def test_index_corresponds_to_position_in_list(self) -> None:
        account = [self._account_record(title="Sample C", accession="ERS33")]
        new = [{"SAMPLE_TITLE": "Sample A"}, {"SAMPLE_TITLE": "Sample B"}, {"SAMPLE_TITLE": "Sample C"}]
        dups = find_duplicate_samples(new, account)
        assert 2 in dups
        assert dups[2]["accession"] == "ERS33"


class TestNormalizeSampleReport:
    """Unit tests for _normalize_sample_report()."""

    def test_title_direct(self) -> None:
        assert _normalize_sample_report({"title": "T", "accession": "ERS1"})["title"] == "T"

    def test_title_sample_title_fallback(self) -> None:
        assert _normalize_sample_report({"sampleTitle": "ST", "accession": "ERS1"})["title"] == "ST"

    def test_alias_direct(self) -> None:
        assert _normalize_sample_report({"alias": "a", "accession": "ERS1"})["alias"] == "a"

    def test_alias_sample_alias_fallback(self) -> None:
        assert _normalize_sample_report({"sampleAlias": "sa", "accession": "ERS1"})["alias"] == "sa"

    def test_accession_direct(self) -> None:
        assert _normalize_sample_report({"accession": "ERS5"})["accession"] == "ERS5"

    def test_accession_sample_accession_fallback(self) -> None:
        assert _normalize_sample_report({"sampleAccession": "ERS99", "accession": ""})["accession"] == "ERS99"

    def test_missing_fields_default_to_empty_string(self) -> None:
        result = _normalize_sample_report({})
        assert result["title"] == ""
        assert result["alias"] == ""
        assert result["accession"] == ""

    def test_status_defaults_to_unknown(self) -> None:
        assert _normalize_sample_report({})["status"] == "UNKNOWN"

    def test_release_status_mapped_to_status(self) -> None:
        assert _normalize_sample_report({"releaseStatus": "PUBLIC"})["status"] == "PUBLIC"


class TestFetchAccountSamples:
    """Unit tests for fetch_account_samples() — verifies it delegates to common correctly."""

    def test_calls_fetch_account_records_with_correct_urls(self, auth: HTTPBasicAuth) -> None:
        with patch("submit_sample.common.fetch_account_records", return_value=[]) as mock_fetch:
            fetch_account_samples(auth, use_test=False)
            kwargs = mock_fetch.call_args.kwargs
            assert kwargs["prod_url"] == _PROD_REPORTS_URL
            assert kwargs["test_url"] == _TEST_REPORTS_URL

    def test_passes_callable_normalizer(self, auth: HTTPBasicAuth) -> None:
        with patch("submit_sample.common.fetch_account_records", return_value=[]) as mock_fetch:
            fetch_account_samples(auth, use_test=False)
            normalizer = mock_fetch.call_args.kwargs.get("normalizer")
            assert callable(normalizer)

    def test_normalizer_handles_title_variants(self, auth: HTTPBasicAuth) -> None:
        captured = {}

        def capture(*args: Any, **kwargs: Any) -> list:
            captured["fn"] = kwargs.get("normalizer")
            return []

        with patch("submit_sample.common.fetch_account_records", side_effect=capture):
            fetch_account_samples(auth)

        fn = captured["fn"]
        assert fn({"title": "Direct", "accession": "ERS1"})["title"] == "Direct"
        assert fn({"sampleTitle": "Fallback", "accession": "ERS2"})["title"] == "Fallback"


# ---------------------------------------------------------------------------
# E. CLI integration tests
# ---------------------------------------------------------------------------

def _make_sample_json(sample: dict[str, Any]) -> str:
    return json.dumps({"samples": [sample]})


def _extract_json(output: str) -> dict[str, Any]:
    """Extract the last top-level JSON object from mixed CLI output."""
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
        raise ValueError(f"No JSON found in output: {output[:200]!r}")
    return json.loads(output[start:end + 1])


_MINIMAL_SCHEMA_YAML = """\
id: https://example.com/test
name: TestSchema
imports:
- linkml:types
prefixes:
  linkml: https://w3id.org/linkml/
default_range: string
classes:
  dh_interface:
    description: A DataHarmonizer interface
  TestSample:
    is_a: dh_interface
    slots:
    - alias
    - TAXON_ID
    - SAMPLE_TITLE
    - SCIENTIFIC_NAME
    slot_usage:
      alias:
        rank: 1
      TAXON_ID:
        rank: 2
      SAMPLE_TITLE:
        rank: 3
      SCIENTIFIC_NAME:
        rank: 4
slots:
  alias:
    title: Sample alias
  TAXON_ID:
    title: Taxon ID
    range: integer
  SAMPLE_TITLE:
    title: Sample title
  SCIENTIFIC_NAME:
    title: Scientific name
"""

_REAL_XSD_DIR = str(Path(__file__).parent / "assets" / "ena_schema")


class TestMainCli:
    """CLI integration tests via typer.testing.CliRunner."""

    _CRED = "submit_sample.common.get_credentials"
    _SUBMIT = "submit_sample.common.submit_xml"
    _LINKML_VALIDATE = "submit_sample.common.validate_against_linkml"

    def _base_args(self, schema_file: str) -> list[str]:
        return ["--linkml", schema_file, "--xsd", _REAL_XSD_DIR]

    def _invoke(self, runner: CliRunner, args: list[str], filename: str, content: str) -> Any:
        with runner.isolated_filesystem():
            Path(filename).write_text(content)
            Path("schema.yaml").write_text(_MINIMAL_SCHEMA_YAML)
            return runner.invoke(
                app, ["--input", filename] + self._base_args("schema.yaml") + args,
                catch_exceptions=False,
            )

    def _invoke_in_fs(self, runner: CliRunner, args: list[str], filename: str, content: str):
        """Context manager version: caller sets up additional mocks inside the with block."""
        # Returns (runner, isolated_fs_context) — used by tests needing extra patches inside fs.
        # For simplicity just use runner.isolated_filesystem() directly in those tests.
        pass

    def test_json_automated_dry_run_exits_0(self, runner: CliRunner, basic_sample: dict[str, Any]) -> None:
        with (
            patch(self._CRED, return_value=("Webin-12345", "pass")),
            patch(self._LINKML_VALIDATE, return_value=(True, [])),
        ):
            result = self._invoke(runner, ["--automated", "--dry-run"], "samples.json", _make_sample_json(basic_sample))
        assert result.exit_code == 0, result.output
        assert "submitted" in _extract_json(result.output)

    def test_duplicate_detected_without_force_skips_submission(
        self, runner: CliRunner, basic_sample: dict[str, Any]
    ) -> None:
        existing = {
            "title": basic_sample["SAMPLE_TITLE"], "alias": basic_sample["alias"],
            "accession": "ERS55555", "secondary_accession": "SAMEA55555", "status": "PRIVATE",
        }
        with runner.isolated_filesystem():
            Path("samples.json").write_text(_make_sample_json(basic_sample))
            Path("schema.yaml").write_text(_MINIMAL_SCHEMA_YAML)
            with (
                patch(self._CRED, return_value=("Webin-12345", "pass")),
                patch("submit_sample.fetch_account_samples", return_value=[existing]),
                patch(self._LINKML_VALIDATE, return_value=(True, [])),
            ):
                result = runner.invoke(
                    app, ["--input", "samples.json"] + self._base_args("schema.yaml"),
                    catch_exceptions=False,
                )
        assert result.exit_code == 0, result.output
        data = _extract_json(result.output)
        assert len(data["duplicates"]) == 1
        assert data["duplicates"][0]["existing_accession"] == "ERS55555"
        assert data["submitted"] == []

    def test_force_flag_with_duplicate_triggers_modify(
        self, runner: CliRunner, basic_sample: dict[str, Any]
    ) -> None:
        existing = {
            "title": basic_sample["SAMPLE_TITLE"], "alias": basic_sample["alias"],
            "accession": "ERS66666", "secondary_accession": "", "status": "PRIVATE",
        }
        receipt_xml = ET.fromstring(
            '<RECEIPT success="true">'
            '<SAMPLE accession="ERS66666" alias="test-sample-001" status="PRIVATE"/>'
            "</RECEIPT>"
        )
        with runner.isolated_filesystem():
            Path("samples.json").write_text(_make_sample_json(basic_sample))
            Path("schema.yaml").write_text(_MINIMAL_SCHEMA_YAML)
            with (
                patch(self._CRED, return_value=("Webin-12345", "pass")),
                patch("submit_sample.fetch_account_samples", return_value=[existing]),
                patch(self._LINKML_VALIDATE, return_value=(True, [])),
                patch(self._SUBMIT, return_value=receipt_xml),
            ):
                result = runner.invoke(
                    app, ["--input", "samples.json"] + self._base_args("schema.yaml") + ["--force"],
                    catch_exceptions=False,
                )
        assert result.exit_code == 0, result.output
        data = _extract_json(result.output)
        assert len(data["modified"]) == 1
        assert data["modified"][0]["accession"] == "ERS66666"

    def test_failed_submission_exits_1(self, runner: CliRunner, basic_sample: dict[str, Any]) -> None:
        import requests as req
        http_err = req.exceptions.HTTPError(response=MagicMock(status_code=500, text="err"))
        with runner.isolated_filesystem():
            Path("samples.json").write_text(_make_sample_json(basic_sample))
            Path("schema.yaml").write_text(_MINIMAL_SCHEMA_YAML)
            with (
                patch(self._CRED, return_value=("Webin-12345", "pass")),
                patch(self._LINKML_VALIDATE, return_value=(True, [])),
                patch(self._SUBMIT, side_effect=http_err),
            ):
                result = runner.invoke(
                    app, ["--input", "samples.json"] + self._base_args("schema.yaml") + ["--automated"],
                    catch_exceptions=False,
                )
        assert result.exit_code == 1

    def test_test_flag_routes_to_test_url(self, runner: CliRunner, basic_sample: dict[str, Any]) -> None:
        receipt_xml = ET.fromstring(
            '<RECEIPT success="true">'
            '<SAMPLE accession="ERS00001" alias="test-sample-001" status="PRIVATE"/>'
            "</RECEIPT>"
        )
        with runner.isolated_filesystem():
            Path("samples.json").write_text(_make_sample_json(basic_sample))
            Path("schema.yaml").write_text(_MINIMAL_SCHEMA_YAML)
            with (
                patch(self._CRED, return_value=("Webin-12345", "pass")),
                patch(self._LINKML_VALIDATE, return_value=(True, [])),
                patch(self._SUBMIT, return_value=receipt_xml) as mock_submit,
            ):
                result = runner.invoke(
                    app, ["--input", "samples.json"] + self._base_args("schema.yaml") + ["--automated", "--test"],
                    catch_exceptions=False,
                )
        assert result.exit_code == 0, result.output
        called_url = mock_submit.call_args[0][0]
        assert "wwwdev" in called_url, f"Expected test URL; got {called_url}"

    def test_no_test_flag_routes_to_production_url(self, runner: CliRunner, basic_sample: dict[str, Any]) -> None:
        receipt_xml = ET.fromstring(
            '<RECEIPT success="true">'
            '<SAMPLE accession="ERS00002" alias="test-sample-001" status="PRIVATE"/>'
            "</RECEIPT>"
        )
        with runner.isolated_filesystem():
            Path("samples.json").write_text(_make_sample_json(basic_sample))
            Path("schema.yaml").write_text(_MINIMAL_SCHEMA_YAML)
            with (
                patch(self._CRED, return_value=("Webin-12345", "pass")),
                patch(self._LINKML_VALIDATE, return_value=(True, [])),
                patch(self._SUBMIT, return_value=receipt_xml) as mock_submit,
            ):
                result = runner.invoke(
                    app, ["--input", "samples.json"] + self._base_args("schema.yaml") + ["--automated"],
                    catch_exceptions=False,
                )
        assert result.exit_code == 0, result.output
        called_url = mock_submit.call_args[0][0]
        assert "wwwdev" not in called_url, f"Expected prod URL; got {called_url}"

    def test_output_flag_writes_results_to_file(self, runner: CliRunner, basic_sample: dict[str, Any]) -> None:
        with runner.isolated_filesystem():
            Path("samples.json").write_text(_make_sample_json(basic_sample))
            Path("schema.yaml").write_text(_MINIMAL_SCHEMA_YAML)
            with (
                patch(self._CRED, return_value=("Webin-12345", "pass")),
                patch(self._LINKML_VALIDATE, return_value=(True, [])),
            ):
                result = runner.invoke(
                    app,
                    ["--input", "samples.json"] + self._base_args("schema.yaml")
                    + ["--automated", "--dry-run", "--output", "results.json"],
                    catch_exceptions=False,
                )
            assert result.exit_code == 0, result.output
            assert Path("results.json").exists()
            data = json.loads(Path("results.json").read_text())
            assert "submitted" in data


# ---------------------------------------------------------------------------
# Parametrized coverage
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("hold_until,expect_hold", [("2027-03-01", True), ("2028-12-31", True), (None, False)])
def test_hold_until_element_conditional(hold_until: str | None, expect_hold: bool) -> None:
    sample = {"alias": "hold-test", "TAXON_ID": 9606, "SAMPLE_TITLE": "Hold Date Test"}
    root = build_submission_xml([sample], hold_until=hold_until)
    hold_el = root.find(".//HOLD")
    if expect_hold:
        assert hold_el is not None
        assert hold_el.get("HoldUntilDate") == hold_until
    else:
        assert hold_el is None


@pytest.mark.parametrize("action", ["ADD", "MODIFY"])
def test_submission_action_element_present(action: str) -> None:
    sample = {"alias": "action-test", "TAXON_ID": 9606, "SAMPLE_TITLE": "Action Test"}
    root = build_submission_xml([sample], action=action)
    xml_str = ET.tostring(root, encoding="unicode")
    assert f"<{action}" in xml_str or f"<{action}/>" in xml_str
    opposite = "MODIFY" if action == "ADD" else "ADD"
    assert f"<{opposite}" not in xml_str
