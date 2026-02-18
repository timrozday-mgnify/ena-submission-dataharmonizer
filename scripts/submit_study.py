#!/usr/bin/env python3
"""Submit studies to ENA via the Webin REST API v2.

Reads a DataHarmonizer JSON export containing study metadata, validates it
against a LinkML schema (SRA_study.yaml) and an XSD schema (SRA.study.xsd),
checks for duplicate studies already registered under the Webin account,
constructs an XML submission document, and submits new studies to ENA.

Usage:
    python scripts/submit_study.py \
        --username Webin-XXXXX --password SECRET \
        --input studies.json \
        --linkml schemas/SRA_study.yaml \
        --xsd assets/ena_schema \
        --test

    # With hold date (private until specified date, max 2 years):
    python scripts/submit_study.py \
        --username Webin-XXXXX --password SECRET \
        --input studies.json \
        --linkml schemas/SRA_study.yaml \
        --xsd assets/ena_schema \
        --hold-until 2028-01-01

    # Log to file:
    python scripts/submit_study.py \
        --username Webin-XXXXX --password SECRET \
        --input studies.json \
        --linkml schemas/SRA_study.yaml \
        --xsd assets/ena_schema \
        --test --log submission.log
"""

import csv
import json
import logging
import os
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Optional

import requests
import typer
import yaml
from requests.auth import HTTPBasicAuth

app = typer.Typer(help="Submit studies to ENA via the Webin REST API v2.")

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

logger = logging.getLogger("submit_study")


def setup_logging(log_file=None):
    """Configure logging to stderr and optionally to a file."""
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
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


# ---------------------------------------------------------------------------
# ENA API helpers
# ---------------------------------------------------------------------------

PROD_URL = "https://www.ebi.ac.uk/ena/submit/webin-v2"
TEST_URL = "https://wwwdev.ebi.ac.uk/ena/submit/webin-v2"


def get_base_url(use_test):
    return TEST_URL if use_test else PROD_URL


ENA_PORTAL_API = "https://www.ebi.ac.uk/ena/portal/api/search"
PROD_REPORTS_URL = "https://www.ebi.ac.uk/ena/submit/report/studies"
TEST_REPORTS_URL = "https://wwwdev.ebi.ac.uk/ena/submit/report/studies"


def search_study_by_title(title, auth):
    """Search the ENA Portal API for a public study matching the given title.

    Returns a list of matching study dicts (may be empty).
    Each dict contains fields like study_accession, study_title, study_alias, etc.
    """
    params = {
        "result": "study",
        "query": f'study_title="{title}"',
        "fields": "study_accession,secondary_study_accession,study_title,study_alias",
        "format": "json",
        "limit": 10,
    }
    req = requests.Request("GET", ENA_PORTAL_API, params=params, auth=auth)
    prepared = req.prepare()
    logger.debug(f"curl -u {auth.username}:*** \"{prepared.url}\"")

    try:
        resp = requests.get(ENA_PORTAL_API, params=params, auth=auth, timeout=60)
        logger.debug(f"ENA Portal API returned {resp.status_code}")
        if resp.status_code == 204:
            # 204 No Content = no matches
            return []
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else "unknown"
        logger.warning(f"ENA Portal API returned HTTP {status} for title query: {title}")
        return []
    except requests.exceptions.RequestException as e:
        logger.warning(f"ENA Portal API request failed: {e}")
        return []


def fetch_private_studies(auth, use_test=False):
    """Fetch private studies from the Webin Reports API.

    Queries the reports endpoint to list all studies under the account,
    then filters to those with PRIVATE status. When use_test is True,
    tries the test endpoint first, then production as fallback.

    Returns a list of study dicts with normalised keys:
    title, alias, accession, secondary_accession, status.
    """
    if use_test:
        urls_to_try = [TEST_REPORTS_URL, PROD_REPORTS_URL]
    else:
        urls_to_try = [PROD_REPORTS_URL]

    for reports_url in urls_to_try:
        logger.info(f"Fetching private studies from: {reports_url}")
        raw = _fetch_private_studies_from_reports(reports_url, auth)
        if raw is None:
            continue
        # Normalise and filter to PRIVATE only
        private = []
        for es_ in raw:
            es = es_.get("report")
            if es is None:
                continue
            private.append({
                "title": (
                    es.get("title")
                    or es.get("studyTitle")
                    or es.get("STUDY_TITLE", "")
                ),
                "alias": es.get("alias") or es.get("studyAlias") or "",
                "accession": (
                    es.get("accession")
                    or es.get("studyAccession")
                    or es.get("report", {}).get("id", "")
                ),
                "secondary_accession": (
                    es.get("secondaryAccession")
                    or es.get("secondaryId", "")
                ),
                "status": "PRIVATE",
            })
        logger.info(
            f"Found {len(raw)} total studies, {len(private)} with PRIVATE status"
        )
        return private

    logger.warning(
        "Could not reach any Webin reports endpoint. "
        "Private study duplicate checking will be skipped."
    )
    return []


def _fetch_private_studies_from_reports(reports_url, auth):
    """Paginated fetch from a single reports endpoint.

    Returns a list of raw study dicts on success, or None if the endpoint
    is unreachable / returns an auth error.
    """
    studies = []
    params = {"status": "PRIVATE", "format": "json", "max-results": 1000, "skip": 0}

    while True:
        try:
            req = requests.Request("GET", reports_url, params=params, auth=auth)
            prepared = req.prepare()
            logger.debug(f"curl -u {auth.username}:*** \"{prepared.url}\"")
            resp = requests.get(reports_url, params=params, auth=auth, timeout=60)
            logger.info(f"Reports API at {reports_url} returned {resp.status_code}")
            resp.raise_for_status()
            page = resp.json()
            if not page:
                break
            studies.extend(page)
            if len(page) < params["max-results"]:
                break
            params["skip"] += params["max-results"]
            logger.info(f"{len(studies)} studies fetched so far")
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else "unknown"
            if status == 404:
                logger.info(f"Reports API at {reports_url} returned 404 — no studies yet")
                return []
            elif status in (401, 403):
                logger.warning(
                    f"Reports API at {reports_url} returned {status} — "
                    f"endpoint may not be available or credentials may differ"
                )
                return None  # signal caller to try next URL
            else:
                logger.warning(f"Reports API at {reports_url} returned HTTP {status}")
                return None
        except requests.exceptions.RequestException as e:
            logger.warning(f"Reports API at {reports_url} failed: {e}")
            return None

    return studies


def submit_xml(base_url, auth, xml_bytes):
    """Submit an XML document to ENA via the Webin v2 synchronous endpoint.

    Returns the parsed receipt XML element tree root.
    """
    url = f"{base_url}/submit"
    headers = {
        "Content-Type": "application/xml",
        "Accept": "application/xml",
    }
    resp = requests.post(url, data=xml_bytes, headers=headers, auth=auth, timeout=120)
    resp.raise_for_status()
    return ET.fromstring(resp.content)


# ---------------------------------------------------------------------------
# LinkML validation
# ---------------------------------------------------------------------------

def load_linkml_schema(linkml_path):
    """Load a LinkML YAML schema and return the parsed dict."""
    with open(linkml_path) as f:
        return yaml.safe_load(f)


def validate_against_linkml(studies, schema):
    """Validate a list of study dicts against the LinkML schema.

    Returns (is_valid, messages) where messages is a list of strings.
    """
    messages = []
    slots = schema.get("slots", {})
    enums = schema.get("enums", {})

    # Find the main class to get required slots and enum ranges
    classes = schema.get("classes", {})
    main_class = None
    for _cls_name, cls_def in classes.items():
        if cls_def.get("is_a") == "dh_interface":
            main_class = cls_def
            break

    if main_class is None:
        messages.append("ERROR: No class with is_a: dh_interface found in LinkML schema")
        return False, messages

    class_slot_names = main_class.get("slots", [])
    messages.append(f"LinkML schema defines {len(class_slot_names)} slots: {', '.join(class_slot_names)}")

    # Build required set and range map
    required_slots = set()
    slot_ranges = {}
    for slot_name in class_slot_names:
        slot_def = slots.get(slot_name, {})
        if slot_def.get("required"):
            required_slots.add(slot_name)
        slot_ranges[slot_name] = slot_def.get("range", "string")

    messages.append(f"Required slots: {', '.join(sorted(required_slots))}")

    is_valid = True

    for i, study in enumerate(studies):
        study_label = study.get("STUDY_TITLE", study.get("alias", f"study[{i}]"))
        messages.append(f"\n--- Validating study: {study_label} ---")

        # Check for unknown fields
        for key in study:
            if key not in class_slot_names and key != "alias":
                messages.append(f"  WARNING: Unknown field '{key}' not in LinkML schema (will be ignored)")

        # Check required fields
        for req in required_slots:
            val = study.get(req)
            if val is None or (isinstance(val, str) and val.strip() == ""):
                messages.append(f"  ERROR: Required field '{req}' is missing or empty")
                is_valid = False
            else:
                messages.append(f"  OK: Required field '{req}' = {repr(val)}")

        # Check enum ranges
        for slot_name in class_slot_names:
            val = study.get(slot_name)
            if val is None:
                continue
            expected_range = slot_ranges.get(slot_name, "string")
            enum_def = enums.get(expected_range)
            if enum_def:
                pv = enum_def.get("permissible_values", {})
                allowed = list(pv.keys())
                if val not in allowed:
                    messages.append(
                        f"  ERROR: Field '{slot_name}' value {repr(val)} "
                        f"not in allowed values: {allowed}"
                    )
                    is_valid = False
                else:
                    messages.append(f"  OK: Field '{slot_name}' = {repr(val)} (valid enum)")
            elif expected_range == "boolean":
                # Accept Python bools, true/false strings, and DataHarmonizer YES/NO
                bool_strings = ("true", "false", "yes", "no")
                if isinstance(val, bool) or (
                    isinstance(val, str) and val.lower() in bool_strings
                ):
                    messages.append(f"  OK: Field '{slot_name}' = {repr(val)} (boolean)")
                else:
                    messages.append(
                        f"  ERROR: Field '{slot_name}' should be boolean, got {repr(val)}"
                    )
                    is_valid = False
            elif expected_range == "integer":
                try:
                    int(val)
                    messages.append(f"  OK: Field '{slot_name}' = {repr(val)} (integer)")
                except (ValueError, TypeError):
                    messages.append(
                        f"  ERROR: Field '{slot_name}' should be integer, got {repr(val)}"
                    )
                    is_valid = False
            else:
                if val is not None:
                    messages.append(f"  OK: Field '{slot_name}' = {repr(val)} (string)")

    return is_valid, messages


# ---------------------------------------------------------------------------
# XML construction and XSD validation
# ---------------------------------------------------------------------------

def build_submission_xml(studies, hold_until=None):
    """Build a WEBIN XML document for submitting studies/projects.

    Each study in the input list is converted to a PROJECT element.
    Returns an ElementTree Element (root).
    """
    webin = ET.Element("WEBIN")

    # SUBMISSION_SET
    submission_set = ET.SubElement(webin, "SUBMISSION_SET")
    submission = ET.SubElement(submission_set, "SUBMISSION")
    submission.set("alias", f"study-submission-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
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
        alias = study.get("alias", study.get("STUDY_TITLE", "").replace(" ", "_")[:50])
        project = ET.SubElement(project_set, "PROJECT")
        project.set("alias", alias)

        # NAME (use CENTER_PROJECT_NAME or alias)
        name_text = study.get("CENTER_PROJECT_NAME", alias)
        if name_text:
            name_el = ET.SubElement(project, "NAME")
            name_el.text = name_text

        # TITLE (required)
        title_el = ET.SubElement(project, "TITLE")
        title_el.text = study.get("STUDY_TITLE", "")

        # DESCRIPTION
        desc_text = study.get("STUDY_ABSTRACT") or study.get("STUDY_DESCRIPTION", "")
        if desc_text:
            desc_el = ET.SubElement(project, "DESCRIPTION")
            desc_el.text = desc_text

        # SUBMISSION_PROJECT (marks as sequencing project)
        sp = ET.SubElement(project, "SUBMISSION_PROJECT")
        ET.SubElement(sp, "SEQUENCING_PROJECT")

        # PROJECT_ATTRIBUTES — include study_type as an attribute
        study_type = study.get("existing_study_type")
        if study_type:
            attrs = ET.SubElement(project, "PROJECT_ATTRIBUTES")
            attr = ET.SubElement(attrs, "PROJECT_ATTRIBUTE")
            tag_el = ET.SubElement(attr, "TAG")
            tag_el.text = "existing_study_type"
            val_el = ET.SubElement(attr, "VALUE")
            val_el.text = study_type

            new_type = study.get("new_study_type")
            if new_type and study_type == "Other":
                attr2 = ET.SubElement(attrs, "PROJECT_ATTRIBUTE")
                tag2 = ET.SubElement(attr2, "TAG")
                tag2.text = "new_study_type"
                val2 = ET.SubElement(attr2, "VALUE")
                val2.text = new_type

    return webin


def xml_to_bytes(root):
    """Serialize an ElementTree element to bytes with XML declaration."""
    tree = ET.ElementTree(root)
    buf = BytesIO()
    tree.write(buf, encoding="UTF-8", xml_declaration=True)
    return buf.getvalue()


def validate_against_xsd(xml_bytes, xsd_dir):
    """Validate XML bytes against ENA.project.xsd.

    The submission XML has a <WEBIN> root wrapping <PROJECT_SET>, so we
    extract the PROJECT_SET fragment and validate it against the
    ENA.project.xsd schema (which defines the PROJECT_SET root element).

    xsd_dir should be a directory containing ENA.project.xsd and its
    dependency SRA.common.xsd. Uses lxml if available for full XSD
    validation. Falls back to basic well-formedness check with
    stdlib xml.etree.
    Returns (is_valid, messages).
    """
    messages = []
    xsd_dir = os.path.abspath(xsd_dir)
    xsd_path = os.path.join(xsd_dir, "ENA.project.xsd")

    if not os.path.isfile(xsd_path):
        messages.append(f"ERROR: ENA.project.xsd not found in {xsd_dir}")
        return False, messages

    common_path = os.path.join(xsd_dir, "SRA.common.xsd")
    if not os.path.isfile(common_path):
        messages.append(
            f"WARNING: SRA.common.xsd not found in {xsd_dir} — "
            "full XSD validation may fail"
        )

    # Try lxml for proper XSD validation
    try:
        from lxml import etree as lxml_etree

        # Parse with base_url so lxml can resolve relative imports (SRA.common.xsd)
        with open(xsd_path, "rb") as f:
            xsd_doc = lxml_etree.parse(f, base_url=f"file://{xsd_dir}/")

        try:
            xsd_schema = lxml_etree.XMLSchema(xsd_doc)

            # Extract PROJECT_SET from the WEBIN envelope for validation
            full_doc = lxml_etree.fromstring(xml_bytes)
            project_set = full_doc.find("PROJECT_SET")
            if project_set is None:
                messages.append("ERROR: No PROJECT_SET element found in XML")
                return False, messages

            # Validate the PROJECT_SET fragment
            if xsd_schema.validate(project_set):
                messages.append("XSD validation passed (lxml)")
                return True, messages
            else:
                for error in xsd_schema.error_log:
                    messages.append(f"XSD ERROR: {error}")
                return False, messages
        except lxml_etree.XMLSchemaParseError as e:
            messages.append(
                f"WARNING: Could not build XSD schema (missing imports?): {e}. "
                "Falling back to basic XML well-formedness check."
            )

    except ImportError:
        messages.append(
            "WARNING: lxml not installed — cannot perform full XSD validation. "
            "Performing basic XML well-formedness check only. "
            "Install lxml for full validation: pip install lxml"
        )

    # Fallback: basic well-formedness check
    try:
        ET.fromstring(xml_bytes)
        messages.append("XML is well-formed (basic check passed)")

        # Perform manual structural checks against what we know
        root = ET.fromstring(xml_bytes)
        project_set = root.find("PROJECT_SET")
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
                messages.append(f"ERROR: PROJECT '{alias}' missing TITLE")
                return False, messages
            sp = proj.find("SUBMISSION_PROJECT")
            if sp is None:
                messages.append(f"ERROR: PROJECT '{alias}' missing SUBMISSION_PROJECT")
                return False, messages
            messages.append(f"OK: PROJECT '{alias}' has required elements")

        return True, messages
    except ET.ParseError as e:
        messages.append(f"ERROR: XML is not well-formed: {e}")
        return False, messages


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------

def find_duplicate_studies(new_studies, auth, private_studies=None):
    """Check new studies against both public and private ENA studies.

    Performs duplicate checking in two phases:
    1. ENA Portal API per-title query — finds public studies (one request
       per study, stops early if all are duplicates)
    2. Webin Reports API private study list — checks remaining unmatched
       studies against private (held) studies by title and alias

    The private_studies list should be pre-fetched via fetch_private_studies()
    and passed in. If None, only public duplicates are checked.

    Returns a dict mapping index in new_studies to the matching existing
    study info.
    """
    duplicates = {}
    total = len(new_studies)

    # ── Phase 1: Query ENA Portal API for public duplicates ──────────
    logger.info("Phase 1: Checking for public duplicates via ENA Portal API...")
    for i, study in enumerate(new_studies):
        new_title = (study.get("STUDY_TITLE") or "").strip()
        if not new_title:
            continue

        logger.info(f"Querying ENA Portal API for: {new_title}")
        matches = search_study_by_title(new_title, auth)

        if matches:
            for m in matches:
                existing_title = (m.get("study_title") or "").strip()
                if existing_title == new_title:
                    duplicates[i] = {
                        "accession": m.get("study_accession", ""),
                        "secondary_accession": m.get("secondary_study_accession", ""),
                        "alias": m.get("study_alias", ""),
                        "title": existing_title,
                        "status": "PUBLIC",
                        "match_reason": f"title '{new_title}' (public)",
                    }
                    logger.info(
                        f"  Duplicate (public): {duplicates[i]['accession']} "
                        f"(alias: {duplicates[i]['alias']})"
                    )
                    break
            else:
                logger.info(f"  No exact title match (got {len(matches)} partial matches)")
        else:
            logger.info(f"  No existing study found")

        # Early exit if all studies are already duplicates
        if len(duplicates) == total:
            logger.info("All studies are duplicates — skipping further checks")
            return duplicates

    # ── Phase 2: Check private studies from Reports API ──────────────
    if not private_studies:
        return duplicates

    private_by_title = {}
    private_by_alias = {}
    for ps in private_studies:
        title = (ps.get("title") or "").strip()
        alias = (ps.get("alias") or "").strip()
        if title:
            private_by_title[title] = ps
        if alias:
            private_by_alias[alias] = ps

    logger.info(
        f"Phase 2: Checking {len(private_studies)} private studies "
        f"for remaining {total - len(duplicates)} unmatched studies..."
    )

    for i, study in enumerate(new_studies):
        if i in duplicates:
            continue

        new_title = (study.get("STUDY_TITLE") or "").strip()
        new_alias = (study.get("alias") or "").strip()

        if not new_title and not new_alias:
            continue

        match = None
        if new_alias and new_alias in private_by_alias:
            ps = private_by_alias[new_alias]
            match = {
                "accession": ps.get("accession", ""),
                "secondary_accession": ps.get("secondary_accession", ""),
                "alias": ps.get("alias", ""),
                "title": ps.get("title", ""),
                "status": "PRIVATE",
                "match_reason": f"alias '{new_alias}' (private)",
            }
        elif new_title and new_title in private_by_title:
            ps = private_by_title[new_title]
            match = {
                "accession": ps.get("accession", ""),
                "secondary_accession": ps.get("secondary_accession", ""),
                "alias": ps.get("alias", ""),
                "title": ps.get("title", ""),
                "status": "PRIVATE",
                "match_reason": f"title '{new_title}' (private)",
            }

        if match:
            duplicates[i] = match
            logger.info(
                f"Duplicate (private): '{new_title}' matches {match['match_reason']} "
                f"-> {match['accession']}"
            )

            if len(duplicates) == total:
                logger.info("All studies are duplicates — skipping further checks")
                return duplicates

    return duplicates


# ---------------------------------------------------------------------------
# Receipt parsing
# ---------------------------------------------------------------------------

def parse_xml_receipt(receipt_root):
    """Parse an ENA XML receipt and extract study/project accessions.

    Returns (success, accessions_list, messages) where accessions_list
    contains dicts with alias, accession, and status.
    """
    success = receipt_root.get("success", "false").lower() == "true"
    accessions = []
    messages = []

    # Collect info/error messages
    msgs_el = receipt_root.find("MESSAGES")
    if msgs_el is not None:
        for info in msgs_el.findall("INFO"):
            messages.append(f"INFO: {info.text}")
        for err in msgs_el.findall("ERROR"):
            messages.append(f"ERROR: {err.text}")

    # Collect project accessions
    for proj in receipt_root.findall("PROJECT"):
        acc_info = {
            "alias": proj.get("alias", ""),
            "accession": proj.get("accession", ""),
            "status": proj.get("status", ""),
            "holdUntilDate": proj.get("holdUntilDate", ""),
        }
        # External accession (ERP study accession)
        ext = proj.find("EXT_ID")
        if ext is not None:
            acc_info["external_accession"] = ext.get("accession", "")
            acc_info["external_type"] = ext.get("type", "")
        accessions.append(acc_info)

    # Also check for STUDY elements (some receipts use STUDY instead of PROJECT)
    for study in receipt_root.findall("STUDY"):
        acc_info = {
            "alias": study.get("alias", ""),
            "accession": study.get("accession", ""),
            "status": study.get("status", ""),
        }
        accessions.append(acc_info)

    return success, accessions, messages


# ---------------------------------------------------------------------------
# Tabular file loading (CSV, TSV, XLS, XLSX)
# ---------------------------------------------------------------------------

def _is_metadata_row(row):
    """Check if a row is a DataHarmonizer metadata/section label row.

    These rows have a single non-empty cell (e.g. "Generic") with the rest empty.
    """
    non_empty = [c for c in row if c is not None and str(c).strip() != ""]
    return len(non_empty) <= 1


def extract_studies_from_tabular(filepath, delimiter=","):
    """Extract study dicts from a CSV or TSV file.

    Handles an optional metadata first row (auto-detected and skipped).
    Returns a list of study dicts.
    """
    with open(filepath, newline="", encoding="utf-8") as f:
        reader = csv.reader(f, delimiter=delimiter)
        rows = list(reader)

    if not rows:
        return []

    idx = 0
    if _is_metadata_row(rows[idx]):
        idx += 1

    if idx >= len(rows):
        return []

    headers = rows[idx]
    idx += 1

    studies = []
    for row in rows[idx:]:
        study = {}
        for col, val in zip(headers, row):
            col = col.strip()
            if col and val is not None and val.strip() != "":
                study[col] = val.strip()
        if study:
            studies.append(study)

    return studies


def extract_studies_from_excel(filepath):
    """Extract study dicts from an XLS or XLSX file.

    Handles an optional metadata first row (auto-detected and skipped).
    Returns a list of study dicts.
    """
    ext = Path(filepath).suffix.lower()

    if ext == ".xls":
        import xlrd
        wb = xlrd.open_workbook(filepath)
        ws = wb.sheet_by_index(0)
        rows = []
        for r in range(ws.nrows):
            rows.append([ws.cell_value(r, c) for c in range(ws.ncols)])
    else:
        import openpyxl
        wb = openpyxl.load_workbook(filepath, read_only=True, data_only=True)
        ws = wb.active
        rows = []
        for row in ws.iter_rows(values_only=True):
            rows.append(list(row))
        wb.close()

    if not rows:
        return []

    idx = 0
    if _is_metadata_row(rows[idx]):
        idx += 1

    if idx >= len(rows):
        return []

    headers = [str(h).strip() if h is not None else "" for h in rows[idx]]
    idx += 1

    studies = []
    for row in rows[idx:]:
        study = {}
        for col, val in zip(headers, row):
            if not col:
                continue
            if val is not None and str(val).strip() != "":
                study[col] = str(val).strip()
        if study:
            studies.append(study)

    return studies


def load_input_file(filepath):
    """Load study data from JSON, CSV, TSV, XLS, or XLSX.

    Returns a list of study dicts, or None if the format is unrecognised.
    """
    ext = Path(filepath).suffix.lower()
    if ext == ".json":
        with open(filepath) as f:
            input_data = json.load(f)
        return extract_studies_from_json(input_data)
    elif ext == ".csv":
        return extract_studies_from_tabular(filepath, delimiter=",")
    elif ext == ".tsv":
        return extract_studies_from_tabular(filepath, delimiter="\t")
    elif ext in (".xlsx", ".xls"):
        return extract_studies_from_excel(filepath)
    return None


# ---------------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------------

def extract_studies_from_json(input_data):
    """Extract a list of study dicts from a DataHarmonizer JSON export.

    DataHarmonizer exports JSON in the form:
        {
          "schema": "...",
          "Container": {
            "<ClassName>s": [ { ... }, ... ]
          }
        }

    Also handles plain lists and dicts with 'studies'/'data' keys.
    Returns a list of study dicts, or None if the format is unrecognised.
    """
    if isinstance(input_data, list):
        return input_data

    if isinstance(input_data, dict):
        # DataHarmonizer Container format
        container = input_data.get("Container")
        if isinstance(container, dict):
            # Find the first list value inside Container
            for key, val in container.items():
                if isinstance(val, list):
                    logger.info(f"Extracted studies from Container.{key}")
                    return val

        # Flat wrapper keys
        if "studies" in input_data:
            return input_data["studies"]
        if "data" in input_data:
            return input_data["data"]

        # Single study object (no wrapper)
        return [input_data]

    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

@app.command()
def main(
    username: str = typer.Option(..., help="Webin submission account ID (e.g. Webin-XXXXX)"),
    password: str = typer.Option(..., help="Webin account password"),
    input_file: Path = typer.Option(
        ..., "--input", exists=True,
        help="Path to study metadata file (JSON, CSV, TSV, XLS, or XLSX)",
    ),
    linkml: Path = typer.Option(
        ..., exists=True,
        help="Path to LinkML YAML schema (e.g. schemas/SRA_study.yaml)",
    ),
    xsd: Path = typer.Option(
        ..., exists=True, file_okay=False, resolve_path=True,
        help="Directory containing ENA.project.xsd and SRA.common.xsd (e.g. assets/ena_schema)",
    ),
    test: bool = typer.Option(False, "--test", help="Use the ENA test service (submissions are discarded daily)"),
    hold_until: Optional[str] = typer.Option(None, "--hold-until", help="Hold studies private until this date (YYYY-MM-DD, max 2 years from now)"),
    log: Optional[Path] = typer.Option(None, help="Path to log file"),
    output: Optional[Path] = typer.Option(None, help="Path to write JSON accession results (default: stdout)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Validate and build XML but do not submit to ENA"),
):
    """Submit studies to ENA via the Webin REST API v2."""
    setup_logging(log)

    env_label = "TEST" if test else "PRODUCTION"
    logger.info(f"ENA Study Submission — environment: {env_label}")
    base_url = get_base_url(test)
    auth = HTTPBasicAuth(username, password)
    logger.debug(f"Auth: {auth}")

    # ── Step 1: Load input file ──────────────────────────────────────────
    logger.info(f"Loading input: {input_file}")
    studies = load_input_file(input_file)
    if studies is None:
        logger.error(
            "Unsupported file format. Supported: .json, .csv, .tsv, .xlsx, .xls"
        )
        sys.exit(1)

    logger.info(f"Loaded {len(studies)} study/studies from input")

    # ── Step 2: Check for duplicates ────────────────────────────────────
    # Fetch private studies from Webin Reports API (catches held/unreleased)
    logger.info("Fetching private studies from Webin account...")
    private_studies = fetch_private_studies(auth, use_test=test)
    for ps in private_studies:
        logger.info(
            f"  Private: {ps['accession']} | alias={ps['alias']} | title={ps['title']}"
        )

    # Check for duplicates (private via reports, public via Portal API)
    logger.info("Checking for duplicate studies...")
    duplicates = find_duplicate_studies(studies, auth, private_studies=private_studies)
    results = {"duplicates": [], "submitted": [], "failed": []}

    if duplicates:
        logger.warning(f"Found {len(duplicates)} duplicate(s) — these will NOT be submitted:")
        for idx, dup_info in duplicates.items():
            study_title = studies[idx].get("STUDY_TITLE", f"study[{idx}]")
            logger.warning(
                f"  DUPLICATE: '{study_title}' matches existing {dup_info['match_reason']} "
                f"(accession: {dup_info['accession']})"
            )
            results["duplicates"].append({
                "input_index": idx,
                "title": study_title,
                "alias": studies[idx].get("alias", ""),
                "existing_accession": dup_info["accession"],
                "existing_secondary_accession": dup_info.get("secondary_accession", ""),
                "match_reason": dup_info["match_reason"],
            })

    # Filter out duplicates
    studies_to_submit = [s for i, s in enumerate(studies) if i not in duplicates]

    if not studies_to_submit:
        logger.info("No new studies to submit (all are duplicates or input is empty)")
        _write_results(results, output)
        return

    logger.info(f"{len(studies_to_submit)} study/studies to submit after duplicate check")

    # ── Step 4: Validate against LinkML ──────────────────────────────────
    logger.info(f"Loading LinkML schema: {linkml}")
    schema = load_linkml_schema(linkml)

    logger.info("Validating input against LinkML schema...")
    linkml_valid, linkml_messages = validate_against_linkml(studies_to_submit, schema)
    for msg in linkml_messages:
        logger.info(f"  {msg}")

    if not linkml_valid:
        logger.error("LinkML validation FAILED — aborting submission")
        sys.exit(1)

    logger.info("LinkML validation PASSED")

    # ── Step 5: Build submission XML ─────────────────────────────────────
    logger.info("Building XML submission document...")
    xml_root = build_submission_xml(studies_to_submit, hold_until=hold_until)
    xml_bytes = xml_to_bytes(xml_root)

    # Log the XML for debugging
    logger.debug("Generated XML:\n" + xml_bytes.decode("utf-8"))
    logger.info(f"XML document size: {len(xml_bytes)} bytes")

    # ── Step 6: Validate against XSD ─────────────────────────────────────
    logger.info(f"Validating XML against XSD: {xsd}")
    xsd_valid, xsd_messages = validate_against_xsd(xml_bytes, xsd)
    for msg in xsd_messages:
        logger.info(f"  {msg}")

    if not xsd_valid:
        logger.error("XSD validation FAILED — aborting submission")
        sys.exit(1)

    logger.info("XSD validation PASSED")

    # ── Step 7: Submit to ENA ────────────────────────────────────────────
    if dry_run:
        logger.info("DRY RUN — skipping actual submission")
        logger.info("Generated XML:\n" + xml_bytes.decode("utf-8"))
        _write_results(results, output)
        return

    logger.info(f"Submitting to ENA ({env_label})...")
    try:
        receipt_root = submit_xml(base_url, auth, xml_bytes)
    except requests.exceptions.HTTPError as e:
        logger.error(f"HTTP error during submission: {e}")
        if e.response is not None:
            logger.error(f"Response body: {e.response.text}")
        sys.exit(1)

    # ── Step 8: Parse receipt ────────────────────────────────────────────
    success, accessions, receipt_messages = parse_xml_receipt(receipt_root)

    for msg in receipt_messages:
        logger.info(f"  Receipt: {msg}")

    if success:
        logger.info("Submission SUCCESSFUL")
        for acc in accessions:
            ext = acc.get("external_accession", "")
            ext_str = f" (study: {ext})" if ext else ""
            logger.info(
                f"  SUBMITTED: alias={acc['alias']} accession={acc['accession']}"
                f" status={acc['status']}{ext_str}"
            )
            results["submitted"].append(acc)
    else:
        logger.error("Submission FAILED")
        # Include the raw XML receipt for debugging
        receipt_xml_str = ET.tostring(receipt_root, encoding="unicode")
        logger.error(f"Receipt XML: {receipt_xml_str}")
        for acc in accessions:
            results["failed"].append(acc)
        sys.exit(1)

    # ── Step 9: Output results ───────────────────────────────────────────
    _write_results(results, output)

    # Summary
    logger.info("=" * 60)
    logger.info("SUBMISSION SUMMARY")
    logger.info(f"  Duplicates (already in ENA): {len(results['duplicates'])}")
    for d in results["duplicates"]:
        logger.info(f"    {d['title']} -> {d['existing_accession']}")
    logger.info(f"  Newly submitted: {len(results['submitted'])}")
    for s in results["submitted"]:
        ext = s.get("external_accession", "")
        logger.info(f"    {s['alias']} -> {s['accession']}" + (f" ({ext})" if ext else ""))
    logger.info("=" * 60)


def _write_results(results, output_path):
    """Write JSON results to file or stdout."""
    json_str = json.dumps(results, indent=2)
    if output_path:
        with open(output_path, "w") as f:
            f.write(json_str + "\n")
        logger.info(f"Results written to {output_path}")
    else:
        print(json_str)


if __name__ == "__main__":
    app()
