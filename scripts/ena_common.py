"""Shared utilities for ENA submission scripts.

Provide logging, credential management, file loading,
LinkML and XSD validation, Reports API access, duplicate
detection, XML serialisation, and result output used by
``submit_study.py``, ``submit_sample.py``, and
``submit_reads.py``.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import sys
import xml.etree.ElementTree as ET
from collections.abc import Callable, Sequence
from io import BytesIO
from pathlib import Path
from typing import Any, Final

import lxml.etree
import pendulum
import requests
from ena_api import WebinClient, WebinConfig
from requests.auth import HTTPBasicAuth

_LOGGER_NAME: Final = "ena_submit"
logger = logging.getLogger(_LOGGER_NAME)


# -------------------------------------------------------------------
# Constants
# -------------------------------------------------------------------

_MAX_HOLD_YEARS: Final = 2


# -------------------------------------------------------------------
# Logging
# -------------------------------------------------------------------

def setup_logging(log_file: Path | None = None) -> None:
    """Configure stderr and optional file logging for the ena_submit logger tree."""
    root = logging.getLogger(_LOGGER_NAME)
    if root.handlers:
        return

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    root.setLevel(logging.DEBUG)

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(logging.INFO)
    stderr_handler.setFormatter(fmt)
    root.addHandler(stderr_handler)

    if log_file:
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)


# -------------------------------------------------------------------
# Credentials
# -------------------------------------------------------------------

def get_credentials() -> tuple[str, str]:
    """Read ENA credentials from ENA_WEBIN and ENA_WEBIN_PASSWORD env vars.

    Returns:
        Tuple of (username, password).

    Raises:
        ValueError: If either variable is unset or empty.
    """
    username = os.environ.get("ENA_WEBIN", "").strip()
    password = os.environ.get("ENA_WEBIN_PASSWORD", "").strip()
    if not username or not password:
        raise ValueError("ENA_WEBIN and ENA_WEBIN_PASSWORD environment variables must be set")
    return username, password


def create_webin_client(test: bool = False) -> WebinClient:
    """Create an authenticated WebinClient using environment credentials."""
    username, password = get_credentials()
    return WebinClient(config=WebinConfig(webin_id=username, password=password, test=test))


# -------------------------------------------------------------------
# XML utilities
# -------------------------------------------------------------------

def xml_to_bytes(root: ET.Element) -> bytes:
    """Serialise an ElementTree element to UTF-8 bytes with XML declaration."""
    buf = BytesIO()
    ET.ElementTree(root).write(buf, encoding="UTF-8", xml_declaration=True)
    return buf.getvalue()


# -------------------------------------------------------------------
# Hold-until date validation
# -------------------------------------------------------------------

def validate_hold_until(hold_until: str) -> pendulum.Date:
    """Parse and validate a hold-until date string (YYYY-MM-DD).

    Raises:
        ValueError: If the date is invalid, in the past, or more than 2 years ahead.
    """
    try:
        hold_date = pendulum.parse(hold_until, exact=True)
    except (ValueError, pendulum.parsing.ParserError):
        raise ValueError(f"Invalid date format: {hold_until!r}. Expected YYYY-MM-DD.") from None

    today = pendulum.today().date()
    max_date = today.add(years=_MAX_HOLD_YEARS)

    if hold_date > max_date:
        raise ValueError(
            f"Hold date {hold_until} is more than {_MAX_HOLD_YEARS} years from today "
            f"({today}). Maximum allowed: {max_date}."
        )
    if hold_date <= today:
        raise ValueError(f"Hold date {hold_until} is not in the future (today is {today}).")

    return hold_date


# -------------------------------------------------------------------
# ENA checklist XML parsing
# -------------------------------------------------------------------

def parse_checklist_units(xml_path: str | Path) -> dict[str, str]:
    """Parse an ENA checklist XML and return a mapping of field name to unit string."""
    units: dict[str, str] = {}
    try:
        tree = ET.parse(str(xml_path))
    except ET.ParseError as exc:
        logger.warning("Could not parse checklist XML %s: %s", xml_path, exc)
        return units

    for field in tree.iter("FIELD"):
        name_el = field.find("NAME")
        if name_el is None or not name_el.text:
            continue
        units_el = field.find("UNITS")
        if units_el is None:
            continue
        unit_el = units_el.find("UNIT")
        if unit_el is None or not unit_el.text:
            continue
        units[name_el.text.strip()] = unit_el.text.strip()

    return units


# -------------------------------------------------------------------
# XSD validation
# -------------------------------------------------------------------

def validate_xml_against_xsd(
    xml_bytes: bytes,
    xsd_dir: str | Path,
    xsd_filename: str,
    fragment_tag: str | None = None,
    fallback_checker: Callable[[bytes, list[str]], tuple[bool, list[str]]] | None = None,
) -> tuple[bool, list[str]]:
    """Validate XML bytes against an XSD schema file in xsd_dir.

    Args:
        fragment_tag: If set, extract this child element from the document before validating.
        fallback_checker: Called when the XSD schema cannot be built (e.g. missing imports).
    """
    messages: list[str] = []
    xsd_root = Path(xsd_dir).resolve()
    xsd_file = xsd_root / xsd_filename

    if not xsd_file.is_file():
        messages.append(f"ERROR: {xsd_filename} not found in {xsd_root}")
        return False, messages

    common_file = xsd_root / "SRA.common.xsd"
    if not common_file.is_file():
        messages.append(f"WARNING: SRA.common.xsd not found in {xsd_root} — full XSD validation may fail")

    with open(xsd_file, "rb") as fh:
        xsd_doc = lxml.etree.parse(fh, base_url=f"file://{xsd_root}/")

    try:
        xsd_schema = lxml.etree.XMLSchema(xsd_doc)
        full_doc = lxml.etree.fromstring(xml_bytes)

        doc_to_validate = full_doc
        if fragment_tag is not None:
            fragment = full_doc.find(fragment_tag)
            if fragment is None:
                messages.append(f"ERROR: No {fragment_tag} element found in XML")
                return False, messages
            doc_to_validate = fragment

        if xsd_schema.validate(doc_to_validate):
            messages.append("XSD validation passed (lxml)")
            return True, messages

        for error in xsd_schema.error_log:
            messages.append(f"XSD ERROR: {error}")
        return False, messages

    except lxml.etree.XMLSchemaParseError as exc:
        messages.append(
            f"WARNING: Could not build XSD schema (missing imports?): {exc}. "
            "Falling back to basic XML well-formedness check."
        )

    if fallback_checker is not None:
        return fallback_checker(xml_bytes, messages)

    try:
        ET.fromstring(xml_bytes)
    except ET.ParseError as parse_exc:
        messages.append(f"ERROR: XML is not well-formed: {parse_exc}")
        return False, messages

    messages.append("XML is well-formed (basic check passed)")
    return True, messages


# -------------------------------------------------------------------
# File loading (CSV, TSV, XLS, XLSX, JSON)
# -------------------------------------------------------------------

def _is_metadata_row(row: Sequence[object]) -> bool:
    """Return True if row looks like a DataHarmonizer label row (at most one non-empty cell)."""
    return sum(1 for c in row if c is not None and str(c).strip()) <= 1


def extract_records_from_tabular(filepath: str | Path, delimiter: str = ",") -> list[dict[str, str]]:
    """Extract record dicts from a CSV or TSV file, skipping any DataHarmonizer metadata row."""
    with open(filepath, newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh, delimiter=delimiter))

    if not rows:
        return []

    idx = 1 if _is_metadata_row(rows[0]) else 0
    if idx >= len(rows):
        return []

    headers = rows[idx]
    return [
        {col.strip(): val.strip() for col, val in zip(headers, row) if col.strip() and val.strip()}
        for row in rows[idx + 1:]
        if any(val.strip() for val in row)
    ]


def extract_records_from_excel(filepath: str | Path) -> list[dict[str, str]]:
    """Extract record dicts from an XLS or XLSX file, skipping any DataHarmonizer metadata row."""
    ext = Path(filepath).suffix.lower()

    if ext == ".xls":
        import xlrd  # noqa: PLC0415
        wb = xlrd.open_workbook(str(filepath))
        ws = wb.sheet_by_index(0)
        rows: list[list[Any]] = [[ws.cell_value(r, c) for c in range(ws.ncols)] for r in range(ws.nrows)]
    else:
        import openpyxl  # noqa: PLC0415
        wb = openpyxl.load_workbook(filepath, read_only=True, data_only=True)
        ws = wb.active
        rows = [list(r) for r in ws.iter_rows(values_only=True)] if ws is not None else []
        wb.close()

    if not rows:
        return []

    idx = 1 if _is_metadata_row(rows[0]) else 0
    if idx >= len(rows):
        return []

    headers = [str(h).strip() if h is not None else "" for h in rows[idx]]
    return [
        {col: str(val).strip() for col, val in zip(headers, row) if col and val is not None and str(val).strip()}
        for row in rows[idx + 1:]
        if any(val is not None and str(val).strip() for val in row)
    ]


def extract_records_from_json(
    input_data: object,
    record_keys: Sequence[str] = ("data",),
) -> list[dict[str, Any]] | None:
    """Extract record dicts from a JSON object in DataHarmonizer or plain list/dict formats."""
    if isinstance(input_data, list):
        return input_data

    if isinstance(input_data, dict):
        container = input_data.get("Container")
        if isinstance(container, dict):
            for key, val in container.items():
                if isinstance(val, list):
                    logger.info("Extracted records from Container.%s", key)
                    return val

        for key in record_keys:
            if key in input_data:
                return input_data[key]

        return [input_data]

    return None


def load_input_file(
    filepath: str | Path,
    json_record_keys: Sequence[str] = ("data",),
) -> list[dict[str, Any]] | None:
    """Load records from JSON, CSV, TSV, XLS, or XLSX. Returns None for unsupported formats."""
    ext = Path(filepath).suffix.lower()
    if ext == ".json":
        with open(filepath) as fh:
            return extract_records_from_json(json.load(fh), json_record_keys)
    if ext == ".csv":
        return extract_records_from_tabular(filepath, delimiter=",")
    if ext == ".tsv":
        return extract_records_from_tabular(filepath, delimiter="\t")
    if ext in (".xlsx", ".xls"):
        return extract_records_from_excel(filepath)
    return None


# -------------------------------------------------------------------
# Reports API
# -------------------------------------------------------------------

def fetch_from_reports_endpoint(
    url: str,
    auth: HTTPBasicAuth,
    max_results: int = 5000,
) -> list[dict[str, Any]] | None:
    """Fetch records from a Webin Reports endpoint. Returns None on auth/network errors."""
    params = {"format": "json", "max-results": max_results}
    logger.debug('curl -u %s:*** "%s"', auth.username, requests.Request("GET", url, params=params).prepare().url)

    try:
        resp = requests.get(url, params=params, auth=auth, timeout=60)
        logger.info("Reports API at %s returned %s", url, resp.status_code)
        resp.raise_for_status()
        return resp.json()

    except requests.exceptions.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "unknown"
        if status == 404:
            logger.info("Reports API at %s returned 404 — no records yet", url)
            return []
        if status in (401, 403):
            logger.warning("Reports API at %s returned %s — endpoint may not be available or credentials may differ", url, status)
            return None
        logger.warning("Reports API at %s returned HTTP %s", url, status)
        return None

    except requests.exceptions.RequestException as exc:
        logger.warning("Reports API at %s failed: %s", url, exc)
        return None


def fetch_account_records(
    auth: HTTPBasicAuth,
    use_test: bool,
    prod_url: str,
    test_url: str,
    normalizer: Callable[[dict[str, Any]], dict[str, str] | None],
    entity_label: str,
    max_results: int = 5000,
) -> list[dict[str, str]]:
    """Fetch and normalise records from the Reports API, trying test endpoint first if use_test."""
    urls = [test_url, prod_url] if use_test else [prod_url]

    for url in urls:
        logger.info("Fetching account %s from: %s", entity_label, url)
        raw = fetch_from_reports_endpoint(url, auth, max_results)
        if raw is None:
            continue

        records: list[dict[str, str]] = []
        for entry in raw:
            report = entry.get("report")
            if report is None:
                continue
            normalized = normalizer(report)
            if normalized is not None:
                records.append(normalized)

        logger.info("Found %d %s in account", len(records), entity_label)
        return records

    logger.warning("Could not reach any Webin reports endpoint. Duplicate checking for %s will be skipped.", entity_label)
    return []


# -------------------------------------------------------------------
# Duplicate detection
# -------------------------------------------------------------------

def find_duplicates_by_alias_title(
    new_records: Sequence[dict[str, Any]],
    account_records: Sequence[dict[str, str]],
    title_field: str,
    entity_label: str,
) -> dict[int, dict[str, str]]:
    """Check new records against existing account records, matching by alias then title."""
    if not account_records:
        return {}

    by_title = {(rec.get("title") or "").strip(): rec for rec in account_records if (rec.get("title") or "").strip()}
    by_alias = {(rec.get("alias") or "").strip(): rec for rec in account_records if (rec.get("alias") or "").strip()}

    total = len(new_records)
    logger.info("Checking %d new %s against %d existing account %s...", total, entity_label, len(account_records), entity_label)

    duplicates: dict[int, dict[str, str]] = {}
    for i, record in enumerate(new_records):
        new_title = (record.get(title_field) or "").strip()
        new_alias = (record.get("alias") or "").strip()

        if not new_title and not new_alias:
            continue

        match = _match_by_alias_title(new_alias, new_title, by_alias, by_title)
        if match is not None:
            duplicates[i] = match
            logger.info("  Duplicate: '%s' matches %s -> %s (%s)",
                        new_title or new_alias, match["match_reason"], match["accession"], match["status"])
            if len(duplicates) == total:
                logger.info("All %s are duplicates — skipping further checks", entity_label)
                return duplicates

    return duplicates


def classify_duplicates(
    records: list[dict[str, Any]],
    duplicates: dict[int, dict[str, Any]],
    *,
    title_field: str,
    force: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Split records into (to_submit, to_modify, duplicate_entries) based on detected duplicates.

    Args:
        records: Full list of input records.
        duplicates: Output of find_duplicates_by_alias_title — maps input index to match info.
        title_field: Key used to extract the human-readable title from each record.
        force: If True, duplicates are added to to_modify with their existing alias.

    Returns:
        to_submit: Records with no duplicate match.
        to_modify: Duplicate records with alias overridden to existing alias (only when force=True).
        duplicate_entries: Summary dicts suitable for results["duplicates"].
    """
    to_submit = [r for i, r in enumerate(records) if i not in duplicates]
    to_modify: list[dict[str, Any]] = []
    duplicate_entries: list[dict[str, Any]] = []

    for idx, dup_info in duplicates.items():
        title = (records[idx].get(title_field) or f"record[{idx}]").strip()
        action_label = "will be re-submitted with MODIFY" if force else "will NOT be submitted"
        logger.warning("DUPLICATE: '%s' matches existing %s (accession: %s) — %s",
                       title, dup_info["match_reason"], dup_info["accession"], action_label)
        duplicate_entries.append({
            "input_index": idx,
            "title": title,
            "alias": records[idx].get("alias", ""),
            "existing_accession": dup_info["accession"],
            "existing_secondary_accession": dup_info.get("secondary_accession", ""),
            "match_reason": dup_info["match_reason"],
        })
        if force:
            record_copy = dict(records[idx])
            if existing_alias := dup_info.get("alias"):
                record_copy["alias"] = existing_alias
            to_modify.append(record_copy)

    return to_submit, to_modify, duplicate_entries


def _match_by_alias_title(
    new_alias: str,
    new_title: str,
    by_alias: dict[str, dict[str, str]],
    by_title: dict[str, dict[str, str]],
) -> dict[str, str] | None:
    if new_alias and new_alias in by_alias:
        rec, reason = by_alias[new_alias], f"alias '{new_alias}'"
    elif new_title and new_title in by_title:
        rec, reason = by_title[new_title], f"title '{new_title}'"
    else:
        return None

    return {
        "accession": rec.get("accession", ""),
        "secondary_accession": rec.get("secondary_accession", ""),
        "alias": rec.get("alias", ""),
        "title": rec.get("title", ""),
        "status": rec.get("status", "UNKNOWN"),
        "match_reason": reason,
    }


# -------------------------------------------------------------------
# Result output
# -------------------------------------------------------------------

def write_results(results: dict[str, list[dict[str, Any]]], output_path: Path | None) -> None:
    """Write JSON results to a file (if output_path given) or stdout."""
    json_str = json.dumps(results, indent=2)
    if output_path:
        with open(output_path, "w") as fh:
            fh.write(json_str + "\n")
        logger.info("Results written to %s", output_path)
    else:
        print(json_str)
