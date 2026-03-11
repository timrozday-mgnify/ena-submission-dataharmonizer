#!/usr/bin/env python3
"""Submit studies to ENA via the Webin REST API v2.

Read a DataHarmonizer export containing study metadata,
validate it against a LinkML schema and an XSD schema,
check for duplicate studies already registered under the
Webin account, construct an XML submission document, and
submit new studies to ENA.

Usage::

Credentials are read from environment variables to avoid
secrets appearing in shell history or process listings::

    export ENA_USERNAME=Webin-XXXXX
    export ENA_PASSWORD=SECRET

Usage::

    python scripts/submit_study.py \\
        --input studies.json \\
        --linkml schemas/SRA_study.yaml \\
        --xsd assets/ena_schema \\
        --test

    # With hold date (max 2 years):
    python scripts/submit_study.py \\
        --input studies.json \\
        --linkml schemas/SRA_study.yaml \\
        --xsd assets/ena_schema \\
        --hold-until 2028-01-01

    # Log to file:
    python scripts/submit_study.py \\
        --input studies.json \\
        --linkml schemas/SRA_study.yaml \\
        --xsd assets/ena_schema \\
        --test --log submission.log
"""

import csv
import json
import logging
import os
import sys
import xml.etree.ElementTree as ET
from collections.abc import Sequence
import pendulum
from io import BytesIO
from pathlib import Path
from typing import Any, Final

import lxml.etree
import requests
import typer
import yaml
from requests.auth import HTTPBasicAuth

app = typer.Typer(
    help="Submit studies to ENA via the Webin REST API v2.",
)

logger = logging.getLogger("submit_study")


# -----------------------------------------------------------
# Logging setup
# -----------------------------------------------------------


def setup_logging(log_file: Path | None = None) -> None:
    """Configure logging to stderr and optionally to a file.

    Args:
        log_file: Path to a log file. If provided, debug-level
            messages are written there in addition to stderr.
    """
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logger.setLevel(logging.DEBUG)

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(logging.INFO)
    stderr_handler.setFormatter(fmt)
    logger.addHandler(stderr_handler)

    if log_file:
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)


# -----------------------------------------------------------
# ENA API helpers
# -----------------------------------------------------------

PROD_URL: Final = (
    "https://www.ebi.ac.uk/ena/submit/webin-v2"
)
TEST_URL: Final = (
    "https://wwwdev.ebi.ac.uk/ena/submit/webin-v2"
)
PROD_REPORTS_URL: Final = (
    "https://www.ebi.ac.uk/ena/submit/report/projects"
)
TEST_REPORTS_URL: Final = (
    "https://wwwdev.ebi.ac.uk/ena/submit/report/projects"
)


def get_base_url(use_test: bool) -> str:
    """Return the ENA submission base URL."""
    return TEST_URL if use_test else PROD_URL


def fetch_account_studies(
    auth: HTTPBasicAuth,
    use_test: bool = False,
    max_results: int = 5000,
) -> list[dict[str, str]]:
    """Fetch all projects from the Webin Reports API.

    Query the reports endpoint to list all projects under
    the authenticated account (all statuses).  When
    *use_test* is True, try the test endpoint first, then
    production as fallback.

    Args:
        auth: HTTP basic-auth credentials.
        use_test: Try the test endpoint before production.
        max_results: Maximum number of results to request
            from the Reports API.

    Returns:
        List of study dicts with normalised keys: title,
        alias, accession, secondary_accession, status.
    """
    if use_test:
        urls_to_try = [TEST_REPORTS_URL, PROD_REPORTS_URL]
    else:
        urls_to_try = [PROD_REPORTS_URL]

    for reports_url in urls_to_try:
        logger.info(
            "Fetching account studies from: %s",
            reports_url,
        )
        raw = _fetch_studies_from_reports(
            reports_url, auth, max_results,
        )
        if raw is None:
            continue

        account_studies: list[dict[str, str]] = []
        for entry in raw:
            es = entry.get("report")
            if es is None:
                continue
            account_studies.append({
                "title": (
                    es.get("title")
                    or es.get("studyTitle")
                    or es.get("STUDY_TITLE", "")
                ),
                "alias": (
                    es.get("alias")
                    or es.get("studyAlias")
                    or ""
                ),
                "accession": (
                    es.get("accession")
                    or es.get("studyAccession")
                    or es.get("report", {}).get("id", "")
                ),
                "secondary_accession": (
                    es.get("secondaryAccession")
                    or es.get("secondaryId", "")
                ),
                "status": es.get(
                    "releaseStatus", "UNKNOWN"
                ),
            })

        logger.info(
            "Found %d studies in account",
            len(account_studies),
        )
        return account_studies

    logger.warning(
        "Could not reach any Webin reports endpoint. "
        "Account study duplicate checking will be skipped.",
    )
    return []


def _fetch_studies_from_reports(
    reports_url: str,
    auth: HTTPBasicAuth,
    max_results: int = 5000,
) -> list[dict[str, Any]] | None:
    """Fetch projects from a single reports endpoint.

    Args:
        reports_url: Full URL of the reports endpoint.
        auth: HTTP basic-auth credentials.
        max_results: Maximum number of results to request.

    Returns:
        List of raw project dicts on success, or ``None``
        if the endpoint is unreachable or returns an auth
        error.
    """
    params = {
        "format": "json",
        "max-results": max_results,
    }

    req = requests.Request(
        "GET", reports_url,
        params=params, auth=auth,
    )
    prepared = req.prepare()
    logger.debug(
        'curl -u %s:*** "%s"',
        auth.username, prepared.url,
    )

    try:
        resp = requests.get(
            reports_url,
            params=params, auth=auth, timeout=60,
        )
        logger.info(
            "Reports API at %s returned %s",
            reports_url, resp.status_code,
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
                " — no projects yet",
                reports_url,
            )
            return []
        if status in (401, 403):
            logger.warning(
                "Reports API at %s returned %s"
                " — endpoint may not be available"
                " or credentials may differ",
                reports_url, status,
            )
            return None
        logger.warning(
            "Reports API at %s returned HTTP %s",
            reports_url, status,
        )
        return None

    except requests.exceptions.RequestException as exc:
        logger.warning(
            "Reports API at %s failed: %s",
            reports_url, exc,
        )
        return None


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
    studies: Sequence[dict[str, Any]],
    schema: dict[str, Any],
) -> tuple[bool, list[str]]:
    """Validate study dicts against a LinkML schema.

    Args:
        studies: Study dicts to validate.
        schema: Parsed LinkML schema dict.

    Returns:
        Tuple of (*is_valid*, *messages*).
    """
    messages: list[str] = []
    slots = schema.get("slots", {})
    enums = schema.get("enums", {})

    # Find the main class (is_a: dh_interface)
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

    for i, study in enumerate(studies):
        study_label = study.get(
            "STUDY_TITLE",
            study.get("alias", f"study[{i}]"),
        )
        messages.append(
            f"\n--- Validating study: {study_label} ---"
        )

        # Unknown fields
        for key in study:
            if key not in class_slot_names and key != "alias":
                messages.append(
                    f"  WARNING: Unknown field '{key}'"
                    " not in LinkML schema"
                    " (will be ignored)"
                )

        # Required fields
        for req in required_slots:
            val = study.get(req)
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

        # Enum and type checks
        for slot_name in class_slot_names:
            val = study.get(slot_name)
            if val is None:
                continue

            expected_range = slot_ranges.get(
                slot_name, "string"
            )
            enum_def = enums.get(expected_range)

            if enum_def:
                pv = enum_def.get("permissible_values", {})
                allowed = list(pv.keys())
                if val not in allowed:
                    messages.append(
                        f"  ERROR: Field '{slot_name}'"
                        f" value {val!r} not in allowed"
                        f" values: {allowed}"
                    )
                    is_valid = False
                else:
                    messages.append(
                        f"  OK: Field '{slot_name}'"
                        f" = {val!r} (valid enum)"
                    )
            elif expected_range == "boolean":
                _BOOL_STRINGS = {
                    "true", "false", "yes", "no",
                }
                if isinstance(val, bool) or (
                    isinstance(val, str)
                    and val.lower() in _BOOL_STRINGS
                ):
                    messages.append(
                        f"  OK: Field '{slot_name}'"
                        f" = {val!r} (boolean)"
                    )
                else:
                    messages.append(
                        f"  ERROR: Field '{slot_name}'"
                        f" should be boolean, got {val!r}"
                    )
                    is_valid = False
            elif expected_range == "integer":
                try:
                    int(val)
                except (ValueError, TypeError):
                    messages.append(
                        f"  ERROR: Field '{slot_name}'"
                        f" should be integer, got {val!r}"
                    )
                    is_valid = False
                else:
                    messages.append(
                        f"  OK: Field '{slot_name}'"
                        f" = {val!r} (integer)"
                    )
            else:
                messages.append(
                    f"  OK: Field '{slot_name}'"
                    f" = {val!r} (string)"
                )

    return is_valid, messages


# -----------------------------------------------------------
# XML construction and XSD validation
# -----------------------------------------------------------

_MAX_HOLD_YEARS: Final = 2


def validate_hold_until(
    hold_until: str,
) -> pendulum.Date:
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


def build_submission_xml(
    studies: Sequence[dict[str, Any]],
    hold_until: str | None = None,
) -> ET.Element:
    """Build a WEBIN XML document for submitting studies.

    Each study in the input list is converted to a PROJECT
    element.

    Args:
        studies: Study metadata dicts.
        hold_until: Optional hold-until date string
            (``YYYY-MM-DD``).

    Returns:
        Root ``<WEBIN>`` element.
    """
    webin = ET.Element("WEBIN")

    # SUBMISSION_SET
    submission_set = ET.SubElement(webin, "SUBMISSION_SET")
    submission = ET.SubElement(
        submission_set, "SUBMISSION",
    )
    alias = (
        "study-submission-"
        + pendulum.now().format("YYYYMMDD-HHmmss")
    )
    submission.set("alias", alias)
    actions = ET.SubElement(submission, "ACTIONS")
    add_action = ET.SubElement(actions, "ACTION")
    ET.SubElement(add_action, "ADD")
    if hold_until:
        hold_action = ET.SubElement(actions, "ACTION")
        hold_el = ET.SubElement(hold_action, "HOLD")
        hold_el.set("HoldUntilDate", hold_until)

    # PROJECT_SET
    project_set = ET.SubElement(webin, "PROJECT_SET")
    for study in studies:
        _add_project_element(project_set, study)

    return webin


def _add_project_element(
    project_set: ET.Element,
    study: dict[str, Any],
) -> None:
    """Append a ``<PROJECT>`` element to *project_set*."""
    alias = study.get(
        "alias",
        study.get("STUDY_TITLE", "").replace(" ", "_")[:50],
    )
    project = ET.SubElement(project_set, "PROJECT")
    project.set("alias", alias)

    # NAME
    name_text = study.get("CENTER_PROJECT_NAME", alias)
    if name_text:
        name_el = ET.SubElement(project, "NAME")
        name_el.text = name_text

    # TITLE (required)
    title_el = ET.SubElement(project, "TITLE")
    title_el.text = study.get("STUDY_TITLE", "")

    # DESCRIPTION
    desc_text = (
        study.get("STUDY_ABSTRACT")
        or study.get("STUDY_DESCRIPTION", "")
    )
    if desc_text:
        desc_el = ET.SubElement(project, "DESCRIPTION")
        desc_el.text = desc_text

    # SUBMISSION_PROJECT
    sp = ET.SubElement(project, "SUBMISSION_PROJECT")
    ET.SubElement(sp, "SEQUENCING_PROJECT")

    # PROJECT_ATTRIBUTES
    study_type = study.get("existing_study_type")
    if study_type:
        attrs = ET.SubElement(project, "PROJECT_ATTRIBUTES")
        _add_project_attribute(
            attrs, "existing_study_type", study_type,
        )
        new_type = study.get("new_study_type")
        if new_type and study_type == "Other":
            _add_project_attribute(
                attrs, "new_study_type", new_type,
            )


def _add_project_attribute(
    parent: ET.Element,
    tag_text: str,
    value_text: str,
) -> None:
    """Append a ``<PROJECT_ATTRIBUTE>`` to *parent*."""
    attr = ET.SubElement(parent, "PROJECT_ATTRIBUTE")
    tag_el = ET.SubElement(attr, "TAG")
    tag_el.text = tag_text
    val_el = ET.SubElement(attr, "VALUE")
    val_el.text = value_text


def xml_to_bytes(root: ET.Element) -> bytes:
    """Serialise an ElementTree element to bytes."""
    tree = ET.ElementTree(root)
    buf = BytesIO()
    tree.write(buf, encoding="UTF-8", xml_declaration=True)
    return buf.getvalue()


def validate_against_xsd(
    xml_bytes: bytes,
    xsd_dir: str | Path,
) -> tuple[bool, list[str]]:
    """Validate XML bytes against ENA.project.xsd.

    Extract the ``<PROJECT_SET>`` fragment from the
    ``<WEBIN>`` envelope and validate it.  Fall back to a
    basic well-formedness check if the XSD schema cannot
    be built.

    Args:
        xml_bytes: Serialised XML document.
        xsd_dir: Directory containing ``ENA.project.xsd``
            and ``SRA.common.xsd``.

    Returns:
        Tuple of (*is_valid*, *messages*).
    """
    messages: list[str] = []
    xsd_root = Path(xsd_dir).resolve()
    xsd_file = xsd_root / "ENA.project.xsd"
    common_file = xsd_root / "SRA.common.xsd"

    if not xsd_file.is_file():
        messages.append(
            f"ERROR: ENA.project.xsd not found in"
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
        project_set = full_doc.find("PROJECT_SET")
        if project_set is None:
            messages.append(
                "ERROR: No PROJECT_SET element found in XML"
            )
            return False, messages

        if xsd_schema.validate(project_set):
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

    return _validate_xml_structure(xml_bytes, messages)


def _validate_xml_structure(
    xml_bytes: bytes,
    messages: list[str],
) -> tuple[bool, list[str]]:
    """Check XML well-formedness and basic structure.

    Used as a fallback when lxml XSD validation is
    unavailable.

    Args:
        xml_bytes: Serialised XML document.
        messages: Accumulator for validation messages.

    Returns:
        Tuple of (*is_valid*, *messages*).
    """
    try:
        tree = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        messages.append(
            f"ERROR: XML is not well-formed: {exc}"
        )
        return False, messages

    messages.append("XML is well-formed (basic check passed)")

    project_set = tree.find("PROJECT_SET")
    if project_set is None:
        messages.append("ERROR: Missing PROJECT_SET element")
        return False, messages

    projects = project_set.findall("PROJECT")
    if not projects:
        messages.append("ERROR: No PROJECT elements found")
        return False, messages

    for proj in projects:
        alias = proj.get("alias", "<no alias>")
        title = proj.find("TITLE")
        if title is None or not title.text:
            messages.append(
                f"ERROR: PROJECT '{alias}' missing TITLE"
            )
            return False, messages
        sp = proj.find("SUBMISSION_PROJECT")
        if sp is None:
            messages.append(
                f"ERROR: PROJECT '{alias}'"
                " missing SUBMISSION_PROJECT"
            )
            return False, messages
        messages.append(
            f"OK: PROJECT '{alias}' has required elements"
        )

    return True, messages


# -----------------------------------------------------------
# Duplicate detection
# -----------------------------------------------------------


def find_duplicate_studies(
    new_studies: Sequence[dict[str, Any]],
    account_studies: Sequence[dict[str, str]],
) -> dict[int, dict[str, str]]:
    """Check new studies against existing account studies.

    Match by alias (preferred) or title against the
    pre-fetched *account_studies* list from the Webin
    Reports API.

    Args:
        new_studies: Studies the user wants to submit.
        account_studies: Existing studies already
            registered under the Webin account.

    Returns:
        Mapping of index in *new_studies* to matching
        existing study info.
    """
    duplicates: dict[int, dict[str, str]] = {}
    total = len(new_studies)

    if not account_studies:
        return duplicates

    by_title: dict[str, dict[str, str]] = {}
    by_alias: dict[str, dict[str, str]] = {}
    for ps in account_studies:
        title = (ps.get("title") or "").strip()
        alias = (ps.get("alias") or "").strip()
        if title:
            by_title[title] = ps
        if alias:
            by_alias[alias] = ps

    logger.info(
        "Checking %d new studies against"
        " %d existing account studies...",
        total, len(account_studies),
    )

    for i, study in enumerate(new_studies):
        new_title = (
            study.get("STUDY_TITLE") or ""
        ).strip()
        new_alias = (study.get("alias") or "").strip()

        if not new_title and not new_alias:
            continue

        match = _match_study(
            new_alias, new_title, by_alias, by_title,
        )
        if match:
            duplicates[i] = match
            logger.info(
                "  Duplicate: '%s' matches %s"
                " -> %s (%s)",
                new_title, match["match_reason"],
                match["accession"], match["status"],
            )

            if len(duplicates) == total:
                logger.info(
                    "All studies are duplicates"
                    " — skipping further checks",
                )
                return duplicates

    return duplicates


def _match_study(
    new_alias: str,
    new_title: str,
    by_alias: dict[str, dict[str, str]],
    by_title: dict[str, dict[str, str]],
) -> dict[str, str] | None:
    """Return matching study info or ``None``."""
    if new_alias and new_alias in by_alias:
        ps = by_alias[new_alias]
        reason = f"alias '{new_alias}'"
    elif new_title and new_title in by_title:
        ps = by_title[new_title]
        reason = f"title '{new_title}'"
    else:
        return None

    return {
        "accession": ps.get("accession", ""),
        "secondary_accession": ps.get(
            "secondary_accession", ""
        ),
        "alias": ps.get("alias", ""),
        "title": ps.get("title", ""),
        "status": ps.get("status", "UNKNOWN"),
        "match_reason": reason,
    }


# -----------------------------------------------------------
# Receipt parsing
# -----------------------------------------------------------


def parse_xml_receipt(
    receipt_root: ET.Element,
) -> tuple[bool, list[dict[str, str]], list[str]]:
    """Parse an ENA XML receipt.

    Args:
        receipt_root: Root element of the receipt XML.

    Returns:
        Tuple of (*success*, *accessions*, *messages*).
        Each accession dict contains alias, accession,
        and status.
    """
    success = (
        receipt_root.get("success", "false").lower()
        == "true"
    )
    accessions: list[dict[str, str]] = []
    messages: list[str] = []

    msgs_el = receipt_root.find("MESSAGES")
    if msgs_el is not None:
        for info in msgs_el.findall("INFO"):
            messages.append(f"INFO: {info.text}")
        for err in msgs_el.findall("ERROR"):
            messages.append(f"ERROR: {err.text}")

    for proj in receipt_root.findall("PROJECT"):
        acc_info: dict[str, str] = {
            "alias": proj.get("alias", ""),
            "accession": proj.get("accession", ""),
            "status": proj.get("status", ""),
            "holdUntilDate": proj.get(
                "holdUntilDate", ""
            ),
        }
        ext = proj.find("EXT_ID")
        if ext is not None:
            acc_info["external_accession"] = ext.get(
                "accession", ""
            )
            acc_info["external_type"] = ext.get(
                "type", ""
            )
        accessions.append(acc_info)

    # Some receipts use STUDY instead of PROJECT
    for study in receipt_root.findall("STUDY"):
        accessions.append({
            "alias": study.get("alias", ""),
            "accession": study.get("accession", ""),
            "status": study.get("status", ""),
        })

    return success, accessions, messages


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


def extract_studies_from_tabular(
    filepath: str | Path,
    delimiter: str = ",",
) -> list[dict[str, str]]:
    """Extract study dicts from a CSV or TSV file.

    Skip an optional DataHarmonizer metadata row if
    detected.

    Args:
        filepath: Path to the tabular file.
        delimiter: Column delimiter character.

    Returns:
        List of study dicts.
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

    studies: list[dict[str, str]] = []
    for row in rows[idx:]:
        study: dict[str, str] = {}
        for col, val in zip(headers, row):
            col = col.strip()
            if col and val is not None and val.strip():
                study[col] = val.strip()
        if study:
            studies.append(study)

    return studies


def extract_studies_from_excel(
    filepath: str | Path,
) -> list[dict[str, str]]:
    """Extract study dicts from an XLS or XLSX file.

    Skip an optional DataHarmonizer metadata row if
    detected.

    Args:
        filepath: Path to the spreadsheet file.

    Returns:
        List of study dicts.
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

    studies: list[dict[str, str]] = []
    for row in rows[idx:]:
        study: dict[str, str] = {}
        for col, val in zip(headers, row):
            if not col:
                continue
            if val is not None and str(val).strip():
                study[col] = str(val).strip()
        if study:
            studies.append(study)

    return studies


def load_input_file(
    filepath: str | Path,
) -> list[dict[str, Any]] | None:
    """Load study data from a supported file format.

    Supported formats: JSON, CSV, TSV, XLS, XLSX.

    Args:
        filepath: Path to the input file.

    Returns:
        List of study dicts, or ``None`` if the format is
        unrecognised.
    """
    ext = Path(filepath).suffix.lower()
    if ext == ".json":
        with open(filepath) as fh:
            input_data = json.load(fh)
        return extract_studies_from_json(input_data)
    if ext == ".csv":
        return extract_studies_from_tabular(
            filepath, delimiter=","
        )
    if ext == ".tsv":
        return extract_studies_from_tabular(
            filepath, delimiter="\t"
        )
    if ext in (".xlsx", ".xls"):
        return extract_studies_from_excel(filepath)
    return None


def extract_studies_from_json(
    input_data: object,
) -> list[dict[str, Any]] | None:
    """Extract study dicts from a DataHarmonizer JSON export.

    Handle several JSON shapes:

    * DataHarmonizer Container format::

        {"Container": {"<ClassName>s": [{...}, ...]}}

    * Plain list of dicts.
    * Dict with ``studies`` or ``data`` key.
    * Single study object (no wrapper).

    Args:
        input_data: Parsed JSON data (any shape).

    Returns:
        List of study dicts, or ``None`` if the format is
        unrecognised.
    """
    if isinstance(input_data, list):
        return input_data

    if isinstance(input_data, dict):
        container = input_data.get("Container")
        if isinstance(container, dict):
            for key, val in container.items():
                if isinstance(val, list):
                    logger.info(
                        "Extracted studies from"
                        " Container.%s",
                        key,
                    )
                    return val

        if "studies" in input_data:
            return input_data["studies"]
        if "data" in input_data:
            return input_data["data"]

        return [input_data]

    return None


# -----------------------------------------------------------
# Main
# -----------------------------------------------------------


@app.command()
def main(
    input_file: Path = typer.Option(
        ..., "--input", exists=True,
        help="Path to study metadata file"
        " (JSON, CSV, TSV, XLS, or XLSX)",
    ),
    linkml: Path = typer.Option(
        ..., exists=True,
        help="Path to LinkML YAML schema"
        " (e.g. schemas/SRA_study.yaml)",
    ),
    xsd: Path = typer.Option(
        ..., exists=True,
        file_okay=False, resolve_path=True,
        help="Directory containing ENA.project.xsd"
        " and SRA.common.xsd",
    ),
    test: bool = typer.Option(
        False, "--test",
        help="Use the ENA test service"
        " (submissions are discarded daily)",
    ),
    hold_until: str | None = typer.Option(
        None, "--hold-until",
        help="Hold studies private until this date"
        " (YYYY-MM-DD, max 2 years from now)",
    ),
    log: Path | None = typer.Option(
        None, help="Path to log file",
    ),
    output: Path | None = typer.Option(
        None,
        help="Path to write JSON accession results"
        " (default: stdout)",
    ),
    max_results: int = typer.Option(
        5000, "--max-results",
        help="Maximum number of projects to fetch"
        " from the Reports API for duplicate"
        " checking",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="Validate and build XML but do not"
        " submit to ENA",
    ),
    automated: bool = typer.Option(
        False, "--automated",
        help="Skip duplicate detection against the"
        " Webin Reports API (for automated pipelines)",
    ),
) -> None:
    """Submit studies to ENA via the Webin REST API v2."""
    setup_logging(log)

    username = os.environ.get("ENA_USERNAME", "").strip()
    password = os.environ.get("ENA_PASSWORD", "").strip()
    if not username or not password:
        logger.error(
            "ENA_USERNAME and ENA_PASSWORD environment"
            " variables must be set",
        )
        sys.exit(1)

    env_label = "TEST" if test else "PRODUCTION"
    logger.info(
        "ENA Study Submission — environment: %s",
        env_label,
    )
    base_url = get_base_url(test)
    auth = HTTPBasicAuth(username, password)
    logger.debug("Auth username: %s", username)

    # -- Validate hold-until date ------------------------
    if hold_until:
        validate_hold_until(hold_until)

    # -- Step 1: Load input file -------------------------
    logger.info("Loading input: %s", input_file)
    studies = load_input_file(input_file)
    if studies is None:
        logger.error(
            "Unsupported file format."
            " Supported: .json, .csv, .tsv, .xlsx, .xls",
        )
        sys.exit(1)

    logger.info(
        "Loaded %d study/studies from input",
        len(studies),
    )

    # -- Step 2: Check for duplicates --------------------
    if automated:
        logger.info(
            "Automated mode: skipping duplicate detection",
        )
        duplicates: dict[int, dict[str, Any]] = {}
    else:
        logger.info(
            "Fetching account studies from"
            " Webin Reports API...",
        )
        account_studies = fetch_account_studies(
            auth, use_test=test,
            max_results=max_results,
        )
        for ps in account_studies:
            logger.info(
                "  Account study: %s | alias=%s"
                " | title=%s | status=%s",
                ps["accession"], ps["alias"],
                ps["title"], ps["status"],
            )

        logger.info("Checking for duplicate studies...")
        duplicates = find_duplicate_studies(
            studies, account_studies,
        )
    results: dict[str, list[dict[str, Any]]] = {
        "duplicates": [],
        "submitted": [],
        "failed": [],
    }

    if duplicates:
        logger.warning(
            "Found %d duplicate(s)"
            " — these will NOT be submitted:",
            len(duplicates),
        )
        for idx, dup_info in duplicates.items():
            study_title = studies[idx].get(
                "STUDY_TITLE", f"study[{idx}]",
            )
            logger.warning(
                "  DUPLICATE: '%s' matches existing %s"
                " (accession: %s)",
                study_title,
                dup_info["match_reason"],
                dup_info["accession"],
            )
            results["duplicates"].append({
                "input_index": idx,
                "title": study_title,
                "alias": studies[idx].get("alias", ""),
                "existing_accession": (
                    dup_info["accession"]
                ),
                "existing_secondary_accession": (
                    dup_info.get("secondary_accession", "")
                ),
                "match_reason": dup_info["match_reason"],
            })

    studies_to_submit = [
        s for i, s in enumerate(studies)
        if i not in duplicates
    ]

    if not studies_to_submit:
        logger.info(
            "No new studies to submit"
            " (all are duplicates or input is empty)",
        )
        _write_results(results, output)
        return

    logger.info(
        "%d study/studies to submit"
        " after duplicate check",
        len(studies_to_submit),
    )

    # -- Step 3: Validate against LinkML -----------------
    logger.info("Loading LinkML schema: %s", linkml)
    schema = load_linkml_schema(linkml)

    logger.info("Validating input against LinkML schema...")
    linkml_valid, linkml_messages = (
        validate_against_linkml(studies_to_submit, schema)
    )
    for msg in linkml_messages:
        logger.info("  %s", msg)

    if not linkml_valid:
        logger.error(
            "LinkML validation FAILED — aborting submission",
        )
        sys.exit(1)

    logger.info("LinkML validation PASSED")

    # -- Step 4: Build submission XML --------------------
    logger.info("Building XML submission document...")
    xml_root = build_submission_xml(
        studies_to_submit, hold_until=hold_until,
    )
    xml_bytes = xml_to_bytes(xml_root)

    logger.debug(
        "Generated XML:\n%s", xml_bytes.decode("utf-8"),
    )
    logger.info(
        "XML document size: %d bytes", len(xml_bytes),
    )

    # -- Step 5: Validate against XSD --------------------
    logger.info("Validating XML against XSD: %s", xsd)
    xsd_valid, xsd_messages = validate_against_xsd(
        xml_bytes, xsd,
    )
    for msg in xsd_messages:
        logger.info("  %s", msg)

    if not xsd_valid:
        logger.error(
            "XSD validation FAILED — aborting submission",
        )
        sys.exit(1)

    logger.info("XSD validation PASSED")

    # -- Step 6: Submit to ENA ---------------------------
    if dry_run:
        logger.info("DRY RUN — skipping actual submission")
        logger.info(
            "Generated XML:\n%s",
            xml_bytes.decode("utf-8"),
        )
        _write_results(results, output)
        return

    logger.info("Submitting to ENA (%s)...", env_label)
    try:
        receipt_root = submit_xml(
            base_url, auth, xml_bytes,
        )
    except requests.exceptions.HTTPError as exc:
        logger.error(
            "HTTP error during submission: %s", exc,
        )
        if exc.response is not None:
            logger.error(
                "Response body: %s", exc.response.text,
            )
        sys.exit(1)

    # -- Step 7: Parse receipt ---------------------------
    success, accessions, receipt_messages = (
        parse_xml_receipt(receipt_root)
    )

    for msg in receipt_messages:
        logger.info("  Receipt: %s", msg)

    if success:
        logger.info("Submission SUCCESSFUL")
        for acc in accessions:
            ext = acc.get("external_accession", "")
            ext_suffix = f" (study: {ext})" if ext else ""
            logger.info(
                "  SUBMITTED: alias=%s accession=%s"
                " status=%s%s",
                acc["alias"], acc["accession"],
                acc["status"], ext_suffix,
            )
            results["submitted"].append(acc)
    else:
        logger.error("Submission FAILED")
        receipt_xml_str = ET.tostring(
            receipt_root, encoding="unicode",
        )
        logger.error("Receipt XML: %s", receipt_xml_str)
        results["failed"].extend(accessions)
        sys.exit(1)

    # -- Step 8: Output results --------------------------
    _write_results(results, output)

    logger.info("=" * 60)
    logger.info("SUBMISSION SUMMARY")
    logger.info(
        "  Duplicates (already in ENA): %d",
        len(results["duplicates"]),
    )
    for d in results["duplicates"]:
        logger.info(
            "    %s -> %s",
            d["title"], d["existing_accession"],
        )
    logger.info(
        "  Newly submitted: %d",
        len(results["submitted"]),
    )
    for s in results["submitted"]:
        ext = s.get("external_accession", "")
        ext_suffix = f" ({ext})" if ext else ""
        logger.info(
            "    %s -> %s%s",
            s["alias"], s["accession"], ext_suffix,
        )
    logger.info("=" * 60)


def _write_results(
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


if __name__ == "__main__":
    app()
