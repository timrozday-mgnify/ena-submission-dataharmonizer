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
import typer
import yaml
from requests.auth import HTTPBasicAuth

# All loggers in the ENA submission scripts are children of
# this root, so configuring it once propagates to all.
_LOGGER_NAME: Final = "ena_submit"

logger = logging.getLogger(_LOGGER_NAME)


# -----------------------------------------------------------
# Constants
# -----------------------------------------------------------

PROD_URL: Final = (
    "https://www.ebi.ac.uk/ena/submit/webin-v2"
)
TEST_URL: Final = (
    "https://wwwdev.ebi.ac.uk/ena/submit/webin-v2"
)

_MAX_HOLD_YEARS: Final = 2

_BOOL_STRINGS: Final = frozenset({
    "true", "false", "yes", "no",
})


# -----------------------------------------------------------
# Logging
# -----------------------------------------------------------


def setup_logging(log_file: Path | None = None) -> None:
    """Configure stderr and optional file logging.

    Attach handlers to the ``ena_submit`` parent logger.
    Child loggers (e.g. ``ena_submit.study``) propagate
    their messages to these handlers automatically.

    Args:
        log_file: Path to a log file.  If provided,
            debug-level messages are written there in
            addition to stderr.
    """
    root = logging.getLogger(_LOGGER_NAME)

    # Avoid duplicate handlers on repeated calls.
    if root.handlers:
        return

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
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


# -----------------------------------------------------------
# Credentials
# -----------------------------------------------------------


def get_credentials() -> tuple[str, str]:
    """Read ENA credentials from environment variables.

    Returns:
        Tuple of (*username*, *password*).

    Raises:
        SystemExit: If either variable is unset or empty.
    """
    username = os.environ.get("ENA_USERNAME", "").strip()
    password = os.environ.get("ENA_PASSWORD", "").strip()
    if not username or not password:
        logger.error(
            "ENA_USERNAME and ENA_PASSWORD environment"
            " variables must be set",
        )
        sys.exit(1)
    return username, password


# -----------------------------------------------------------
# ENA API helpers
# -----------------------------------------------------------


def get_base_url(use_test: bool) -> str:
    """Return the ENA Webin v2 submission base URL."""
    return TEST_URL if use_test else PROD_URL


def submit_xml(
    base_url: str,
    auth: HTTPBasicAuth,
    xml_bytes: bytes,
) -> ET.Element:
    """Submit an XML document to ENA via Webin v2.

    Args:
        base_url: ENA submission service base URL.
        auth: HTTP basic-auth credentials.
        xml_bytes: Serialised XML submission document.

    Returns:
        Parsed receipt XML element tree root.
    """
    url = f"{base_url}/submit"
    headers = {
        "Content-Type": "application/xml",
        "Accept": "application/xml",
    }
    resp = requests.post(
        url, data=xml_bytes,
        headers=headers, auth=auth, timeout=120,
    )
    resp.raise_for_status()
    return ET.fromstring(resp.content)


# -----------------------------------------------------------
# XML utilities
# -----------------------------------------------------------


def xml_to_bytes(root: ET.Element) -> bytes:
    """Serialise an ElementTree element to UTF-8 bytes."""
    tree = ET.ElementTree(root)
    buf = BytesIO()
    tree.write(buf, encoding="UTF-8", xml_declaration=True)
    return buf.getvalue()


# -----------------------------------------------------------
# Hold-until date validation
# -----------------------------------------------------------


def validate_hold_until(hold_until: str) -> pendulum.Date:
    """Parse and validate a hold-until date string.

    Args:
        hold_until: Date string in ``YYYY-MM-DD`` format.

    Returns:
        Parsed date.

    Raises:
        typer.BadParameter: If the date format is invalid,
            in the past, or more than 2 years from today.
    """
    try:
        hold_date = pendulum.parse(
            hold_until, exact=True,
        )
    except (ValueError, pendulum.parsing.ParserError):
        raise typer.BadParameter(
            f"Invalid date format: {hold_until!r}."
            " Expected YYYY-MM-DD."
        ) from None

    today = pendulum.today().date()
    max_date = today.add(years=_MAX_HOLD_YEARS)

    if hold_date > max_date:
        raise typer.BadParameter(
            f"Hold date {hold_until} is more than"
            f" {_MAX_HOLD_YEARS} years from today"
            f" ({today}). Maximum allowed: {max_date}."
        )

    if hold_date <= today:
        raise typer.BadParameter(
            f"Hold date {hold_until} is not in the"
            f" future (today is {today})."
        )

    return hold_date


# -----------------------------------------------------------
# LinkML validation
# -----------------------------------------------------------


def load_linkml_schema(
    linkml_path: str | Path,
) -> dict[str, Any]:
    """Load a LinkML YAML schema and return the parsed dict.

    Args:
        linkml_path: Path to the YAML schema file.
    """
    with open(linkml_path) as fh:
        return yaml.safe_load(fh)


def validate_against_linkml(
    records: Sequence[dict[str, Any]],
    schema: dict[str, Any],
    label_fields: Sequence[str] = ("alias",),
    entity_name: str = "record",
    unknown_field_note: str = "will be ignored",
) -> tuple[bool, list[str]]:
    """Validate record dicts against a LinkML schema.

    Check required fields, enum values, boolean/integer
    ranges, and warn about unknown fields.

    Args:
        records: Record dicts to validate.
        schema: Parsed LinkML schema dict.
        label_fields: Field names to try for the per-record
            label (tried in order; falls back to index).
        entity_name: Fallback label prefix (e.g. ``study``).
        unknown_field_note: Appended to unknown-field
            warnings (e.g. ``"will be passed as
            SAMPLE_ATTRIBUTE"``).

    Returns:
        Tuple of (*is_valid*, *messages*).
    """
    messages: list[str] = []
    slots = schema.get("slots", {})
    enums = schema.get("enums", {})

    classes = schema.get("classes", {})
    main_class = None
    for _, cls_def in classes.items():
        if cls_def.get("is_a") == "dh_interface":
            main_class = cls_def
            break

    if main_class is None:
        messages.append(
            "ERROR: No class with is_a: dh_interface"
            " found in LinkML schema"
        )
        return False, messages

    class_slot_names = main_class.get("slots", [])
    messages.append(
        f"LinkML schema defines {len(class_slot_names)}"
        f" slots: {', '.join(class_slot_names)}"
    )

    required_slots: set[str] = set()
    slot_ranges: dict[str, str] = {}
    for slot_name in class_slot_names:
        slot_def = slots.get(slot_name, {})
        if slot_def.get("required"):
            required_slots.add(slot_name)
        slot_ranges[slot_name] = slot_def.get(
            "range", "string"
        )

    messages.append(
        "Required slots: "
        + ", ".join(sorted(required_slots))
    )

    is_valid = True

    for i, record in enumerate(records):
        label = _record_label(
            record, label_fields, entity_name, i,
        )
        messages.append(
            f"\n--- Validating {entity_name}: {label} ---"
        )

        # Unknown fields
        for key in record:
            if key not in class_slot_names and key != "alias":
                messages.append(
                    f"  WARNING: Unknown field '{key}'"
                    " not in LinkML schema"
                    f" ({unknown_field_note})"
                )

        # Required fields
        for req in required_slots:
            val = record.get(req)
            if val is None or (
                isinstance(val, str) and not val.strip()
            ):
                messages.append(
                    f"  ERROR: Required field '{req}'"
                    " is missing or empty"
                )
                is_valid = False
            else:
                messages.append(
                    f"  OK: Required field '{req}'"
                    f" = {val!r}"
                )

        # Enum, boolean, integer, and string checks
        for slot_name in class_slot_names:
            val = record.get(slot_name)
            if val is None:
                continue

            expected_range = slot_ranges.get(
                slot_name, "string"
            )
            enum_def = enums.get(expected_range)

            if enum_def:
                valid, msg = _check_enum(
                    slot_name, val, enum_def,
                )
                messages.append(msg)
                if not valid:
                    is_valid = False
            elif expected_range == "boolean":
                valid, msg = _check_boolean(slot_name, val)
                messages.append(msg)
                if not valid:
                    is_valid = False
            elif expected_range == "integer":
                valid, msg = _check_integer(slot_name, val)
                messages.append(msg)
                if not valid:
                    is_valid = False
            else:
                messages.append(
                    f"  OK: Field '{slot_name}'"
                    f" = {val!r} (string)"
                )

    return is_valid, messages


def _record_label(
    record: dict[str, Any],
    label_fields: Sequence[str],
    entity_name: str,
    index: int,
) -> str:
    """Return a human-readable label for a record."""
    for field in label_fields:
        val = record.get(field)
        if val:
            return str(val)
    return f"{entity_name}[{index}]"


def _check_enum(
    slot_name: str,
    val: object,
    enum_def: dict[str, Any],
) -> tuple[bool, str]:
    """Check whether *val* is a permissible enum value."""
    pv = enum_def.get("permissible_values", {})
    allowed = list(pv.keys())
    if val not in allowed:
        return False, (
            f"  ERROR: Field '{slot_name}'"
            f" value {val!r} not in allowed"
            f" values: {allowed}"
        )
    return True, (
        f"  OK: Field '{slot_name}'"
        f" = {val!r} (valid enum)"
    )


def _check_boolean(
    slot_name: str,
    val: object,
) -> tuple[bool, str]:
    """Check whether *val* is a valid boolean representation."""
    if isinstance(val, bool) or (
        isinstance(val, str)
        and val.lower() in _BOOL_STRINGS
    ):
        return True, (
            f"  OK: Field '{slot_name}'"
            f" = {val!r} (boolean)"
        )
    return False, (
        f"  ERROR: Field '{slot_name}'"
        f" should be boolean, got {val!r}"
    )


def _check_integer(
    slot_name: str,
    val: object,
) -> tuple[bool, str]:
    """Check whether *val* is a valid integer."""
    try:
        int(val)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return False, (
            f"  ERROR: Field '{slot_name}'"
            f" should be integer, got {val!r}"
        )
    return True, (
        f"  OK: Field '{slot_name}'"
        f" = {val!r} (integer)"
    )


# -----------------------------------------------------------
# XSD validation
# -----------------------------------------------------------


def validate_xml_against_xsd(
    xml_bytes: bytes,
    xsd_dir: str | Path,
    xsd_filename: str,
    fragment_tag: str | None = None,
    fallback_checker: Callable[
        [bytes, list[str]], tuple[bool, list[str]]
    ] | None = None,
) -> tuple[bool, list[str]]:
    """Validate XML bytes against an XSD schema.

    Args:
        xml_bytes: Serialised XML document.
        xsd_dir: Directory containing the XSD file and
            ``SRA.common.xsd``.
        xsd_filename: Name of the XSD file (e.g.
            ``ENA.project.xsd``).
        fragment_tag: If set, extract this child element
            from the parsed XML before validating.
        fallback_checker: Optional function called when the
            XSD schema cannot be built.  Receives
            (*xml_bytes*, *messages*) and returns
            (*is_valid*, *messages*).

    Returns:
        Tuple of (*is_valid*, *messages*).
    """
    messages: list[str] = []
    xsd_root = Path(xsd_dir).resolve()
    xsd_file = xsd_root / xsd_filename
    common_file = xsd_root / "SRA.common.xsd"

    if not xsd_file.is_file():
        messages.append(
            f"ERROR: {xsd_filename} not found in"
            f" {xsd_root}"
        )
        return False, messages

    if not common_file.is_file():
        messages.append(
            f"WARNING: SRA.common.xsd not found in"
            f" {xsd_root}"
            " — full XSD validation may fail"
        )

    with open(xsd_file, "rb") as fh:
        xsd_doc = lxml.etree.parse(
            fh, base_url=f"file://{xsd_root}/",
        )

    try:
        xsd_schema = lxml.etree.XMLSchema(xsd_doc)
        full_doc = lxml.etree.fromstring(xml_bytes)

        doc_to_validate = full_doc
        if fragment_tag is not None:
            fragment = full_doc.find(fragment_tag)
            if fragment is None:
                messages.append(
                    f"ERROR: No {fragment_tag} element"
                    " found in XML"
                )
                return False, messages
            doc_to_validate = fragment

        if xsd_schema.validate(doc_to_validate):
            messages.append(
                "XSD validation passed (lxml)"
            )
            return True, messages

        for error in xsd_schema.error_log:
            messages.append(f"XSD ERROR: {error}")
        return False, messages

    except lxml.etree.XMLSchemaParseError as exc:
        messages.append(
            f"WARNING: Could not build XSD schema"
            f" (missing imports?): {exc}."
            " Falling back to basic XML"
            " well-formedness check."
        )

    if fallback_checker is not None:
        return fallback_checker(xml_bytes, messages)

    # Default: check well-formedness only.
    try:
        ET.fromstring(xml_bytes)
    except ET.ParseError as parse_exc:
        messages.append(
            f"ERROR: XML is not well-formed: {parse_exc}"
        )
        return False, messages

    messages.append(
        "XML is well-formed (basic check passed)"
    )
    return True, messages


# -----------------------------------------------------------
# File loading (CSV, TSV, XLS, XLSX, JSON)
# -----------------------------------------------------------


def _is_metadata_row(row: Sequence[object]) -> bool:
    """Check whether *row* is a DataHarmonizer label row.

    These rows have at most one non-empty cell.
    """
    non_empty = sum(
        1 for c in row
        if c is not None and str(c).strip()
    )
    return non_empty <= 1


def extract_records_from_tabular(
    filepath: str | Path,
    delimiter: str = ",",
) -> list[dict[str, str]]:
    """Extract record dicts from a CSV or TSV file.

    Skip an optional DataHarmonizer metadata row if
    detected.

    Args:
        filepath: Path to the tabular file.
        delimiter: Column delimiter character.

    Returns:
        List of record dicts.
    """
    with open(filepath, newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh, delimiter=delimiter))

    if not rows:
        return []

    idx = 0
    if _is_metadata_row(rows[idx]):
        idx += 1
    if idx >= len(rows):
        return []

    headers = rows[idx]
    idx += 1

    records: list[dict[str, str]] = []
    for row in rows[idx:]:
        record: dict[str, str] = {}
        for col, val in zip(headers, row):
            col = col.strip()
            if col and val is not None and val.strip():
                record[col] = val.strip()
        if record:
            records.append(record)

    return records


def extract_records_from_excel(
    filepath: str | Path,
) -> list[dict[str, str]]:
    """Extract record dicts from an XLS or XLSX file.

    Skip an optional DataHarmonizer metadata row if
    detected.

    Args:
        filepath: Path to the spreadsheet file.

    Returns:
        List of record dicts.
    """
    ext = Path(filepath).suffix.lower()

    if ext == ".xls":
        import xlrd  # noqa: PLC0415

        wb = xlrd.open_workbook(str(filepath))
        ws = wb.sheet_by_index(0)
        rows: list[list[Any]] = [
            [ws.cell_value(r, c) for c in range(ws.ncols)]
            for r in range(ws.nrows)
        ]
    else:
        import openpyxl  # noqa: PLC0415

        wb = openpyxl.load_workbook(
            filepath, read_only=True, data_only=True,
        )
        ws = wb.active
        rows = (
            [
                list(r)
                for r in ws.iter_rows(values_only=True)
            ]
            if ws is not None
            else []
        )
        wb.close()

    if not rows:
        return []

    idx = 0
    if _is_metadata_row(rows[idx]):
        idx += 1
    if idx >= len(rows):
        return []

    headers = [
        str(h).strip() if h is not None else ""
        for h in rows[idx]
    ]
    idx += 1

    records: list[dict[str, str]] = []
    for row in rows[idx:]:
        record: dict[str, str] = {}
        for col, val in zip(headers, row):
            if not col:
                continue
            if val is not None and str(val).strip():
                record[col] = str(val).strip()
        if record:
            records.append(record)

    return records


def extract_records_from_json(
    input_data: object,
    record_keys: Sequence[str] = ("data",),
) -> list[dict[str, Any]] | None:
    """Extract record dicts from a DataHarmonizer JSON export.

    Handle several JSON shapes:

    * DataHarmonizer Container format::

        {"Container": {"<ClassName>s": [{...}, ...]}}

    * Plain list of dicts.
    * Dict with an entity-specific key or ``data`` key.
    * Single record object (no wrapper).

    Args:
        input_data: Parsed JSON data (any shape).
        record_keys: Dict keys to check for record lists
            (e.g. ``["studies", "data"]``).

    Returns:
        List of record dicts, or ``None`` if unrecognised.
    """
    if isinstance(input_data, list):
        return input_data

    if isinstance(input_data, dict):
        container = input_data.get("Container")
        if isinstance(container, dict):
            for key, val in container.items():
                if isinstance(val, list):
                    logger.info(
                        "Extracted records from"
                        " Container.%s",
                        key,
                    )
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
    """Load records from a supported file format.

    Supported formats: JSON, CSV, TSV, XLS, XLSX.

    Args:
        filepath: Path to the input file.
        json_record_keys: Dict keys to check when parsing
            JSON (e.g. ``["studies", "data"]``).

    Returns:
        List of record dicts, or ``None`` if the format is
        unrecognised.
    """
    ext = Path(filepath).suffix.lower()
    if ext == ".json":
        with open(filepath) as fh:
            input_data = json.load(fh)
        return extract_records_from_json(
            input_data, json_record_keys,
        )
    if ext == ".csv":
        return extract_records_from_tabular(
            filepath, delimiter=",",
        )
    if ext == ".tsv":
        return extract_records_from_tabular(
            filepath, delimiter="\t",
        )
    if ext in (".xlsx", ".xls"):
        return extract_records_from_excel(filepath)
    return None


# -----------------------------------------------------------
# Reports API
# -----------------------------------------------------------


def fetch_from_reports_endpoint(
    url: str,
    auth: HTTPBasicAuth,
    max_results: int = 5000,
) -> list[dict[str, Any]] | None:
    """Fetch records from a single Webin Reports endpoint.

    Args:
        url: Full URL of the reports endpoint.
        auth: HTTP basic-auth credentials.
        max_results: Maximum number of results to request.

    Returns:
        List of raw report dicts, or ``None`` on error.
    """
    params = {
        "format": "json",
        "max-results": max_results,
    }

    req = requests.Request(
        "GET", url, params=params, auth=auth,
    )
    prepared = req.prepare()
    logger.debug(
        'curl -u %s:*** "%s"',
        auth.username, prepared.url,
    )

    try:
        resp = requests.get(
            url, params=params, auth=auth, timeout=60,
        )
        logger.info(
            "Reports API at %s returned %s",
            url, resp.status_code,
        )
        resp.raise_for_status()
        return resp.json()

    except requests.exceptions.HTTPError as exc:
        status = (
            exc.response.status_code
            if exc.response is not None
            else "unknown"
        )
        if status == 404:
            logger.info(
                "Reports API at %s returned 404"
                " — no records yet",
                url,
            )
            return []
        if status in (401, 403):
            logger.warning(
                "Reports API at %s returned %s"
                " — endpoint may not be available"
                " or credentials may differ",
                url, status,
            )
            return None
        logger.warning(
            "Reports API at %s returned HTTP %s",
            url, status,
        )
        return None

    except requests.exceptions.RequestException as exc:
        logger.warning(
            "Reports API at %s failed: %s", url, exc,
        )
        return None


def fetch_account_records(
    auth: HTTPBasicAuth,
    use_test: bool,
    prod_url: str,
    test_url: str,
    normalizer: Callable[
        [dict[str, Any]], dict[str, str] | None
    ],
    entity_label: str,
    max_results: int = 5000,
) -> list[dict[str, str]]:
    """Fetch and normalise records from the Reports API.

    Try test endpoint first (if *use_test*), then fall back
    to production.

    Args:
        auth: HTTP basic-auth credentials.
        use_test: Try the test endpoint first.
        prod_url: Production reports endpoint URL.
        test_url: Test reports endpoint URL.
        normalizer: Callable that maps a raw report dict to
            a normalised dict, or ``None`` to skip.
        entity_label: Label for log messages (e.g.
            ``"studies"``).
        max_results: Maximum number of results to request.

    Returns:
        List of normalised record dicts.
    """
    urls = (
        [test_url, prod_url] if use_test
        else [prod_url]
    )

    for url in urls:
        logger.info(
            "Fetching account %s from: %s",
            entity_label, url,
        )
        raw = fetch_from_reports_endpoint(
            url, auth, max_results,
        )
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

        logger.info(
            "Found %d %s in account",
            len(records), entity_label,
        )
        return records

    logger.warning(
        "Could not reach any Webin reports endpoint."
        " Duplicate checking for %s will be skipped.",
        entity_label,
    )
    return []


# -----------------------------------------------------------
# Duplicate detection (alias + title matching)
# -----------------------------------------------------------


def find_duplicates_by_alias_title(
    new_records: Sequence[dict[str, Any]],
    account_records: Sequence[dict[str, str]],
    title_field: str,
    entity_label: str,
) -> dict[int, dict[str, str]]:
    """Check new records against account records.

    Match by ``alias`` (preferred) or by the entity-specific
    title field against the pre-fetched account records from
    the Webin Reports API.

    Args:
        new_records: Records the user wants to submit.
        account_records: Existing records already registered
            under the Webin account.
        title_field: Field name for the title in new records
            (e.g. ``"STUDY_TITLE"`` or ``"SAMPLE_TITLE"``).
        entity_label: Label for log messages.

    Returns:
        Mapping of index in *new_records* to matching
        existing record info.
    """
    duplicates: dict[int, dict[str, str]] = {}
    total = len(new_records)

    if not account_records:
        return duplicates

    by_title: dict[str, dict[str, str]] = {}
    by_alias: dict[str, dict[str, str]] = {}
    for rec in account_records:
        title = (rec.get("title") or "").strip()
        alias = (rec.get("alias") or "").strip()
        if title:
            by_title[title] = rec
        if alias:
            by_alias[alias] = rec

    logger.info(
        "Checking %d new %s against"
        " %d existing account %s...",
        total, entity_label,
        len(account_records), entity_label,
    )

    for i, record in enumerate(new_records):
        new_title = (
            record.get(title_field) or ""
        ).strip()
        new_alias = (record.get("alias") or "").strip()

        if not new_title and not new_alias:
            continue

        match = _match_by_alias_title(
            new_alias, new_title, by_alias, by_title,
        )
        if match is not None:
            duplicates[i] = match
            logger.info(
                "  Duplicate: '%s' matches %s -> %s (%s)",
                new_title or new_alias,
                match["match_reason"],
                match["accession"],
                match["status"],
            )

            if len(duplicates) == total:
                logger.info(
                    "All %s are duplicates"
                    " — skipping further checks",
                    entity_label,
                )
                return duplicates

    return duplicates


def _match_by_alias_title(
    new_alias: str,
    new_title: str,
    by_alias: dict[str, dict[str, str]],
    by_title: dict[str, dict[str, str]],
) -> dict[str, str] | None:
    """Return matching record info or ``None``."""
    if new_alias and new_alias in by_alias:
        rec = by_alias[new_alias]
        reason = f"alias '{new_alias}'"
    elif new_title and new_title in by_title:
        rec = by_title[new_title]
        reason = f"title '{new_title}'"
    else:
        return None

    return {
        "accession": rec.get("accession", ""),
        "secondary_accession": rec.get(
            "secondary_accession", ""
        ),
        "alias": rec.get("alias", ""),
        "title": rec.get("title", ""),
        "status": rec.get("status", "UNKNOWN"),
        "match_reason": reason,
    }


# -----------------------------------------------------------
# Result output
# -----------------------------------------------------------


def write_results(
    results: dict[str, list[dict[str, Any]]],
    output_path: Path | None,
) -> None:
    """Write JSON results to file or stdout."""
    json_str = json.dumps(results, indent=2)
    if output_path:
        with open(output_path, "w") as fh:
            fh.write(json_str + "\n")
        logger.info("Results written to %s", output_path)
    else:
        print(json_str)
