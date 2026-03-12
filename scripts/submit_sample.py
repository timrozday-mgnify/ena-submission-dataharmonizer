#!/usr/bin/env python3
"""Submit samples to ENA via the Webin REST API v2.

Read a DataHarmonizer export containing sample metadata,
validate it against a LinkML schema and an XSD schema,
check for duplicate samples already registered under the
Webin account, construct an XML submission document, and
submit new samples to ENA.

Credentials are read from environment variables to avoid
secrets appearing in shell history or process listings::

    export ENA_USERNAME=Webin-XXXXX
    export ENA_PASSWORD=SECRET

Usage::

    python scripts/submit_sample.py \\
        --input samples.json \\
        --linkml schemas/ERC000015.yaml \\
        --xsd assets/ena_schema \\
        --test

    # With hold date (max 2 years):
    python scripts/submit_sample.py \\
        --input samples.json \\
        --linkml schemas/ERC000015.yaml \\
        --xsd assets/ena_schema \\
        --hold-until 2028-01-01

    # Log to file:
    python scripts/submit_sample.py \\
        --input samples.json \\
        --linkml schemas/ERC000015.yaml \\
        --xsd assets/ena_schema \\
        --test --log submission.log
"""

from __future__ import annotations

import logging
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Final

import pendulum
import requests
import typer
from requests.auth import HTTPBasicAuth

import ena_common as common

app = typer.Typer(
    help="Submit samples to ENA via the Webin REST API v2.",
)

logger = logging.getLogger("ena_submit.sample")

# Fields consumed as dedicated XML elements rather than
# SAMPLE_ATTRIBUTE tag-value pairs.
_RESERVED_FIELDS: Final = frozenset({
    "alias",
    "SAMPLE_TITLE",
    "TAXON_ID",
    "SCIENTIFIC_NAME",
    "COMMON_NAME",
    "SAMPLE_DESCRIPTION",
    "SAMPLE_ABSTRACT",
})


# -----------------------------------------------------------
# Reports API (sample-specific)
# -----------------------------------------------------------

_PROD_REPORTS_URL: Final = (
    "https://www.ebi.ac.uk/ena/submit/report/samples"
)
_TEST_REPORTS_URL: Final = (
    "https://wwwdev.ebi.ac.uk/ena/submit/report/samples"
)


def _normalize_sample_report(
    report: dict[str, Any],
) -> dict[str, str]:
    """Normalise a raw sample report dict."""
    return {
        "title": (
            report.get("title")
            or report.get("sampleTitle")
            or report.get("SAMPLE_TITLE", "")
        ),
        "alias": (
            report.get("alias")
            or report.get("sampleAlias")
            or ""
        ),
        "accession": (
            report.get("accession")
            or report.get("sampleAccession")
            or ""
        ),
        "secondary_accession": (
            report.get("secondaryAccession")
            or report.get("secondaryId", "")
        ),
        "status": report.get(
            "releaseStatus", "UNKNOWN"
        ),
    }


def fetch_account_samples(
    auth: HTTPBasicAuth,
    use_test: bool = False,
    max_results: int = 5000,
) -> list[dict[str, str]]:
    """Fetch all samples from the Webin Reports API.

    Args:
        auth: HTTP basic-auth credentials.
        use_test: Try the test endpoint before production.
        max_results: Maximum number of results to request.

    Returns:
        List of normalised sample dicts.
    """
    return common.fetch_account_records(
        auth,
        use_test=use_test,
        prod_url=_PROD_REPORTS_URL,
        test_url=_TEST_REPORTS_URL,
        normalizer=_normalize_sample_report,
        entity_label="samples",
        max_results=max_results,
    )


def find_duplicate_samples(
    new_samples: list[dict[str, Any]],
    account_samples: list[dict[str, str]],
) -> dict[int, dict[str, str]]:
    """Check new samples against existing account samples.

    Args:
        new_samples: Samples the user wants to submit.
        account_samples: Existing samples in the account.

    Returns:
        Mapping of index to matching sample info.
    """
    return common.find_duplicates_by_alias_title(
        new_samples, account_samples,
        title_field="SAMPLE_TITLE",
        entity_label="samples",
    )


# -----------------------------------------------------------
# XML construction
# -----------------------------------------------------------


def build_submission_xml(
    samples: list[dict[str, Any]],
    hold_until: str | None = None,
    checklist_id: str | None = None,
    slot_to_title: dict[str, str] | None = None,
    slot_to_unit: dict[str, str] | None = None,
    action: str = "ADD",
) -> ET.Element:
    """Build a WEBIN XML document for submitting samples.

    Each sample in the input list is converted to a SAMPLE
    element.  Fields not consumed by dedicated XML elements
    are emitted as ``<SAMPLE_ATTRIBUTE>`` tag-value pairs,
    with tag names translated back to their human-readable
    titles using *slot_to_title* and units appended where
    defined in *slot_to_unit*.

    Args:
        samples: Sample metadata dicts.
        hold_until: Optional hold-until date string
            (``YYYY-MM-DD``).
        checklist_id: ENA checklist identifier to embed as
            an ``ENA-CHECKLIST`` SAMPLE_ATTRIBUTE
            (e.g. ``"ERC000015"``).
        slot_to_title: Mapping from slot name to human-
            readable title used as the XML TAG value.
            Keys absent from this map are used as-is.
        slot_to_unit: Mapping from slot name to unit string
            (e.g. ``{"geographic_location_latitude": "DD"}``).
            When present, a ``<UNITS>`` child is added to the
            ``<SAMPLE_ATTRIBUTE>`` element.
        action: Submission action — ``"ADD"`` for new samples
            or ``"MODIFY"`` to update existing ones.

    Returns:
        Root ``<WEBIN>`` element.
    """
    webin = ET.Element("WEBIN")

    submission_set = ET.SubElement(webin, "SUBMISSION_SET")
    submission = ET.SubElement(
        submission_set, "SUBMISSION",
    )
    sub_alias = (
        "sample-submission-"
        + pendulum.now().format("YYYYMMDD-HHmmss")
    )
    submission.set("alias", sub_alias)
    actions = ET.SubElement(submission, "ACTIONS")
    main_action = ET.SubElement(actions, "ACTION")
    ET.SubElement(main_action, action.upper())
    if hold_until:
        hold_action = ET.SubElement(actions, "ACTION")
        hold_el = ET.SubElement(hold_action, "HOLD")
        hold_el.set("HoldUntilDate", hold_until)

    sample_set = ET.SubElement(webin, "SAMPLE_SET")
    for sample in samples:
        _add_sample_element(
            sample_set, sample, checklist_id,
            slot_to_title, slot_to_unit,
        )

    return webin


def _add_sample_element(
    sample_set: ET.Element,
    sample: dict[str, Any],
    checklist_id: str | None = None,
    slot_to_title: dict[str, str] | None = None,
    slot_to_unit: dict[str, str] | None = None,
) -> None:
    """Append a ``<SAMPLE>`` element to *sample_set*.

    Args:
        sample_set: Parent ``<SAMPLE_SET>`` element.
        sample: Sample metadata dict (keys are slot names).
        checklist_id: If provided, added as an
            ``ENA-CHECKLIST`` SAMPLE_ATTRIBUTE.
        slot_to_title: Mapping from slot name to the
            human-readable title used as the XML TAG value.
            ENA's checklist validator requires these titles.
        slot_to_unit: Mapping from slot name to unit string.
            When present for a field, a ``<UNITS>`` child
            element is added to its ``<SAMPLE_ATTRIBUTE>``.
    """
    alias = sample.get(
        "alias",
        (
            sample.get("SAMPLE_TITLE") or ""
        ).replace(" ", "_")[:50],
    )
    sample_el = ET.SubElement(sample_set, "SAMPLE")
    sample_el.set("alias", alias)

    title = sample.get("SAMPLE_TITLE", "")
    if title:
        title_el = ET.SubElement(sample_el, "TITLE")
        title_el.text = title

    # SAMPLE_NAME (required by XSD; TAXON_ID is mandatory)
    sample_name = ET.SubElement(
        sample_el, "SAMPLE_NAME",
    )
    taxon_el = ET.SubElement(sample_name, "TAXON_ID")
    taxon_el.text = str(sample.get("TAXON_ID", ""))
    sci_name = sample.get("SCIENTIFIC_NAME", "")
    if sci_name:
        sci_el = ET.SubElement(
            sample_name, "SCIENTIFIC_NAME",
        )
        sci_el.text = sci_name
    common_name = sample.get("COMMON_NAME", "")
    if common_name:
        common_el = ET.SubElement(
            sample_name, "COMMON_NAME",
        )
        common_el.text = common_name

    desc = (
        sample.get("SAMPLE_DESCRIPTION")
        or sample.get("SAMPLE_ABSTRACT", "")
    )
    if desc:
        desc_el = ET.SubElement(sample_el, "DESCRIPTION")
        desc_el.text = desc

    # Remaining fields become SAMPLE_ATTRIBUTEs.
    # ENA-CHECKLIST is always first when present.
    attrs = {
        k: v for k, v in sample.items()
        if k not in _RESERVED_FIELDS
        and v is not None
        and str(v).strip()
    }
    if attrs or checklist_id:
        attrs_el = ET.SubElement(
            sample_el, "SAMPLE_ATTRIBUTES",
        )
        if checklist_id:
            _add_sample_attribute(
                attrs_el, "ENA-CHECKLIST", checklist_id,
            )
        for tag, value in attrs.items():
            tag_name = (slot_to_title or {}).get(tag, tag)
            unit = (slot_to_unit or {}).get(tag)
            _add_sample_attribute(
                attrs_el, tag_name, str(value), unit,
            )


def _add_sample_attribute(
    parent: ET.Element,
    tag_text: str,
    value_text: str,
    unit: str | None = None,
) -> None:
    """Append a ``<SAMPLE_ATTRIBUTE>`` to *parent*.

    Args:
        parent: Parent ``<SAMPLE_ATTRIBUTES>`` element.
        tag_text: Value for the ``<TAG>`` child.
        value_text: Value for the ``<VALUE>`` child.
        unit: If provided, added as a ``<UNITS>`` child.
    """
    attr = ET.SubElement(parent, "SAMPLE_ATTRIBUTE")
    tag_el = ET.SubElement(attr, "TAG")
    tag_el.text = tag_text
    val_el = ET.SubElement(attr, "VALUE")
    val_el.text = value_text
    if unit:
        units_el = ET.SubElement(attr, "UNITS")
        units_el.text = unit


# -----------------------------------------------------------
# XSD validation (sample-specific fallback)
# -----------------------------------------------------------


def _validate_sample_xml_structure(
    xml_bytes: bytes,
    messages: list[str],
) -> tuple[bool, list[str]]:
    """Fallback structural check for sample XML."""
    try:
        tree = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        messages.append(
            f"ERROR: XML is not well-formed: {exc}"
        )
        return False, messages

    messages.append(
        "XML is well-formed (basic check passed)"
    )

    sample_set = tree.find("SAMPLE_SET")
    if sample_set is None:
        messages.append(
            "ERROR: Missing SAMPLE_SET element"
        )
        return False, messages

    samples = sample_set.findall("SAMPLE")
    if not samples:
        messages.append("ERROR: No SAMPLE elements found")
        return False, messages

    for sample in samples:
        alias = sample.get("alias", "<no alias>")
        sample_name = sample.find("SAMPLE_NAME")
        if sample_name is None:
            messages.append(
                f"ERROR: SAMPLE '{alias}'"
                " missing SAMPLE_NAME"
            )
            return False, messages
        taxon = sample_name.find("TAXON_ID")
        if taxon is None or not taxon.text:
            messages.append(
                f"ERROR: SAMPLE '{alias}'"
                " missing TAXON_ID"
            )
            return False, messages
        messages.append(
            f"OK: SAMPLE '{alias}' has required elements"
        )

    return True, messages


def validate_against_xsd(
    xml_bytes: bytes,
    xsd_dir: str | Path,
) -> tuple[bool, list[str]]:
    """Validate sample XML against SRA.sample.xsd.

    Args:
        xml_bytes: Serialised XML document.
        xsd_dir: Directory containing ``SRA.sample.xsd``
            and ``SRA.common.xsd``.

    Returns:
        Tuple of (*is_valid*, *messages*).
    """
    return common.validate_xml_against_xsd(
        xml_bytes, xsd_dir,
        xsd_filename="SRA.sample.xsd",
        fragment_tag="SAMPLE_SET",
        fallback_checker=_validate_sample_xml_structure,
    )


# -----------------------------------------------------------
# Receipt parsing
# -----------------------------------------------------------


def parse_xml_receipt(
    receipt_root: ET.Element,
) -> tuple[bool, list[dict[str, str]], list[str]]:
    """Parse an ENA XML receipt for sample submissions.

    Args:
        receipt_root: Root element of the receipt XML.

    Returns:
        Tuple of (*success*, *accessions*, *messages*).
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

    for sample in receipt_root.findall("SAMPLE"):
        acc_info: dict[str, str] = {
            "alias": sample.get("alias", ""),
            "accession": sample.get("accession", ""),
            "status": sample.get("status", ""),
            "holdUntilDate": sample.get(
                "holdUntilDate", ""
            ),
        }
        ext = sample.find("EXT_ID")
        if ext is not None:
            acc_info["external_accession"] = ext.get(
                "accession", ""
            )
            acc_info["external_type"] = ext.get(
                "type", ""
            )
        accessions.append(acc_info)

    return success, accessions, messages


# -----------------------------------------------------------
# Submission helper
# -----------------------------------------------------------


def _do_submission(
    base_url: str,
    auth: Any,
    xml_bytes: bytes,
    xsd: Path,
    action: str,
    results: dict[str, list[dict[str, Any]]],
    result_key: str,
    env_label: str,
    dry_run: bool,
) -> bool:
    """Validate, optionally submit, and parse one batch.

    Validates *xml_bytes* against the XSD, optionally submits
    it to ENA, parses the receipt, and appends accession dicts
    to ``results[result_key]`` or ``results["failed"]``.

    Args:
        base_url: ENA Webin v2 submission base URL.
        auth: HTTP basic-auth credentials.
        xml_bytes: Serialised XML submission document.
        xsd: Directory containing the XSD files.
        action: Human-readable label for log messages
            (e.g. ``"ADD"`` or ``"MODIFY"``).
        results: Results dict to accumulate into.
        result_key: Key under which successes are stored
            (e.g. ``"submitted"`` or ``"modified"``).
        env_label: ``"TEST"`` or ``"PRODUCTION"``.
        dry_run: If ``True``, skip the actual submission.

    Returns:
        ``True`` if the batch succeeded (or dry run).
    """
    xsd_valid, xsd_messages = validate_against_xsd(
        xml_bytes, xsd,
    )
    for msg in xsd_messages:
        logger.info("  %s", msg)
    if not xsd_valid:
        logger.error(
            "XSD validation FAILED (%s)"
            " — aborting submission", action,
        )
        return False

    logger.info("XSD validation PASSED (%s)", action)

    if dry_run:
        logger.info(
            "DRY RUN — skipping %s submission", action,
        )
        logger.info(
            "Generated XML:\n%s",
            xml_bytes.decode("utf-8"),
        )
        return True

    logger.info(
        "Submitting %s to ENA (%s)...", action, env_label,
    )
    try:
        receipt_root = common.submit_xml(
            base_url, auth, xml_bytes,
        )
    except requests.exceptions.HTTPError as exc:
        logger.error(
            "HTTP error during %s submission: %s",
            action, exc,
        )
        if exc.response is not None:
            logger.error(
                "Response body: %s", exc.response.text,
            )
        return False

    success, accessions, receipt_messages = (
        parse_xml_receipt(receipt_root)
    )
    for msg in receipt_messages:
        logger.info("  Receipt: %s", msg)

    if success:
        logger.info("%s SUCCESSFUL", action)
        for acc in accessions:
            ext = acc.get("external_accession", "")
            ext_suffix = (
                f" (biosample: {ext})" if ext else ""
            )
            logger.info(
                "  %s: alias=%s accession=%s"
                " status=%s%s",
                action, acc["alias"], acc["accession"],
                acc["status"], ext_suffix,
            )
            results[result_key].append(acc)
    else:
        logger.error("%s FAILED", action)
        receipt_xml_str = ET.tostring(
            receipt_root, encoding="unicode",
        )
        logger.error("Receipt XML: %s", receipt_xml_str)
        results["failed"].extend(accessions)

    return success


# -----------------------------------------------------------
# Main
# -----------------------------------------------------------

_JSON_RECORD_KEYS: Final = ("samples", "data")


@app.command()
def main(
    input_file: Path = typer.Option(
        ..., "--input", exists=True,
        help="Path to sample metadata file"
        " (JSON, CSV, TSV, XLS, or XLSX)",
    ),
    linkml: Path = typer.Option(
        ..., exists=True,
        help="Path to LinkML YAML schema"
        " (e.g. schemas/ERC000015.yaml)",
    ),
    xsd: Path = typer.Option(
        ..., exists=True,
        file_okay=False, resolve_path=True,
        help="Directory containing SRA.sample.xsd"
        " and SRA.common.xsd",
    ),
    test: bool = typer.Option(
        False, "--test",
        help="Use the ENA test service"
        " (submissions are discarded daily)",
    ),
    hold_until: str | None = typer.Option(
        None, "--hold-until",
        help="Hold samples private until this date"
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
        help="Maximum number of samples to fetch"
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
    force: bool = typer.Option(
        False, "--force",
        help="Submit duplicate samples using the MODIFY"
        " action to overwrite existing ENA records,"
        " instead of skipping them",
    ),
) -> None:
    """Submit samples to ENA via the Webin REST API v2."""
    common.setup_logging(log)
    username, password = common.get_credentials()

    env_label = "TEST" if test else "PRODUCTION"
    logger.info(
        "ENA Sample Submission — environment: %s",
        env_label,
    )
    base_url = common.get_base_url(test)
    auth = HTTPBasicAuth(username, password)
    logger.debug("Auth username: %s", username)

    if hold_until:
        common.validate_hold_until(hold_until)

    # -- Step 1: Load input file -------------------------
    logger.info("Loading input: %s", input_file)
    samples = common.load_input_file(
        input_file, json_record_keys=_JSON_RECORD_KEYS,
    )
    if samples is None:
        logger.error(
            "Unsupported file format."
            " Supported: .json, .csv, .tsv, .xlsx, .xls",
        )
        sys.exit(1)

    logger.info(
        "Loaded %d sample(s) from input", len(samples),
    )

    # -- Step 1b: Load LinkML schema and remap keys ------
    # DataHarmonizer exports use slot titles as column
    # headers (e.g. "project name") rather than slot names
    # (e.g. "project_name").  Remap before any downstream
    # processing so that duplicate detection, validation,
    # and XML building all receive canonical slot names.
    logger.info("Loading LinkML schema: %s", linkml)
    schema = common.load_linkml_schema(linkml)
    samples = common.remap_records_by_title(samples, schema)
    logger.info(
        "Remapped record keys using slot titles from schema",
    )

    # -- Step 2: Check for duplicates --------------------
    if automated:
        logger.info(
            "Automated mode: skipping duplicate detection",
        )
        duplicates: dict[int, dict[str, Any]] = {}
    else:
        account_samples = fetch_account_samples(
            auth, use_test=test,
            max_results=max_results,
        )
        for ps in account_samples:
            logger.info(
                "  Account sample: %s | alias=%s"
                " | title=%s | status=%s",
                ps["accession"], ps["alias"],
                ps["title"], ps["status"],
            )
        duplicates = find_duplicate_samples(
            samples, account_samples,
        )

    results: dict[str, list[dict[str, Any]]] = {
        "duplicates": [],
        "submitted": [],
        "modified": [],
        "failed": [],
    }

    # Build the MODIFY list (duplicates with --force) and
    # log all duplicates regardless.
    samples_to_modify: list[dict[str, Any]] = []
    if duplicates:
        action_label = (
            "will be re-submitted with MODIFY"
            if force else "will NOT be submitted"
        )
        logger.warning(
            "Found %d duplicate(s) — %s:",
            len(duplicates), action_label,
        )
        for idx, dup_info in duplicates.items():
            sample_title = samples[idx].get(
                "SAMPLE_TITLE", f"sample[{idx}]",
            )
            logger.warning(
                "  DUPLICATE: '%s' matches existing %s"
                " (accession: %s)",
                sample_title,
                dup_info["match_reason"],
                dup_info["accession"],
            )
            results["duplicates"].append({
                "input_index": idx,
                "title": sample_title,
                "alias": samples[idx].get("alias", ""),
                "existing_accession": (
                    dup_info["accession"]
                ),
                "existing_secondary_accession": (
                    dup_info.get(
                        "secondary_accession", ""
                    )
                ),
                "match_reason": dup_info["match_reason"],
            })
            if force:
                # Use the existing ENA alias so MODIFY
                # targets the correct record.
                sample_copy = dict(samples[idx])
                existing_alias = dup_info.get("alias", "")
                if existing_alias:
                    sample_copy["alias"] = existing_alias
                samples_to_modify.append(sample_copy)

    samples_to_submit = [
        s for i, s in enumerate(samples)
        if i not in duplicates
    ]

    if not samples_to_submit and not samples_to_modify:
        logger.info(
            "No samples to submit"
            " (all are duplicates or input is empty)",
        )
        common.write_results(results, output)
        return

    logger.info(
        "%d new sample(s) to ADD,"
        " %d duplicate(s) to MODIFY",
        len(samples_to_submit), len(samples_to_modify),
    )

    # -- Step 3: Validate against LinkML -----------------
    logger.info(
        "Validating input against LinkML schema...",
    )
    linkml_valid, linkml_messages = (
        common.validate_against_linkml(
            samples_to_submit + samples_to_modify, schema,
            label_fields=["SAMPLE_TITLE", "alias"],
            entity_name="sample",
            unknown_field_note=(
                "will be passed as SAMPLE_ATTRIBUTE"
            ),
        )
    )
    for msg in linkml_messages:
        logger.info("  %s", msg)

    if not linkml_valid:
        logger.error(
            "LinkML validation FAILED"
            " — aborting submission",
        )
        sys.exit(1)

    logger.info("LinkML validation PASSED")

    # -- Step 4: Prepare shared XML building state -------
    schema_name = schema.get("name", "")
    checklist_id: str | None = (
        schema_name
        if re.match(r"^ERC\d+$", schema_name)
        else None
    )
    if checklist_id:
        logger.info(
            "Checklist detected from schema: %s",
            checklist_id,
        )
    else:
        logger.info(
            "No ERC checklist detected in schema name '%s'"
            " — ENA-CHECKLIST attribute will be omitted",
            schema_name,
        )

    slot_to_title = common.build_slot_to_title_map(schema)

    slot_to_unit: dict[str, str] = {}
    if checklist_id:
        checklist_xml = xsd / f"{checklist_id}.xml"
        if checklist_xml.is_file():
            slot_to_unit = common.parse_checklist_units(
                checklist_xml,
            )
            logger.info(
                "Loaded units for %d field(s) from %s",
                len(slot_to_unit), checklist_xml,
            )
        else:
            logger.info(
                "Checklist XML not found at %s"
                " — UNITS elements will be omitted",
                checklist_xml,
            )

    def _build_xml(
        batch: list[dict[str, Any]],
        batch_action: str,
    ) -> bytes:
        xml_root = build_submission_xml(
            batch, hold_until=hold_until,
            checklist_id=checklist_id,
            slot_to_title=slot_to_title,
            slot_to_unit=slot_to_unit,
            action=batch_action,
        )
        xml_bytes = common.xml_to_bytes(xml_root)
        logger.debug(
            "Generated XML (%s):\n%s",
            batch_action, xml_bytes.decode("utf-8"),
        )
        logger.info(
            "XML document size (%s): %d bytes",
            batch_action, len(xml_bytes),
        )
        return xml_bytes

    overall_ok = True

    # -- Step 5/6/7: ADD new samples ---------------------
    if samples_to_submit:
        logger.info(
            "Building ADD XML for %d new sample(s)...",
            len(samples_to_submit),
        )
        add_xml = _build_xml(samples_to_submit, "ADD")
        ok = _do_submission(
            base_url, auth, add_xml, xsd,
            action="ADD",
            results=results,
            result_key="submitted",
            env_label=env_label,
            dry_run=dry_run,
        )
        overall_ok = overall_ok and ok

    # -- Step 5/6/7: MODIFY duplicate samples (--force) --
    if samples_to_modify:
        logger.info(
            "Building MODIFY XML for %d duplicate(s)...",
            len(samples_to_modify),
        )
        mod_xml = _build_xml(samples_to_modify, "MODIFY")
        ok = _do_submission(
            base_url, auth, mod_xml, xsd,
            action="MODIFY",
            results=results,
            result_key="modified",
            env_label=env_label,
            dry_run=dry_run,
        )
        overall_ok = overall_ok and ok

    if not overall_ok:
        sys.exit(1)

    # -- Step 8: Output results --------------------------
    common.write_results(results, output)

    logger.info("=" * 60)
    logger.info("SUBMISSION SUMMARY")
    logger.info(
        "  Duplicates skipped: %d",
        len(results["duplicates"])
        - len(results["modified"]),
    )
    for d in results["duplicates"]:
        logger.info(
            "    %s -> %s",
            d["title"], d["existing_accession"],
        )
    logger.info(
        "  Newly submitted (ADD): %d",
        len(results["submitted"]),
    )
    for s in results["submitted"]:
        ext = s.get("external_accession", "")
        ext_suffix = f" ({ext})" if ext else ""
        logger.info(
            "    %s -> %s%s",
            s["alias"], s["accession"], ext_suffix,
        )
    logger.info(
        "  Modified (MODIFY): %d",
        len(results["modified"]),
    )
    for m in results["modified"]:
        ext = m.get("external_accession", "")
        ext_suffix = f" ({ext})" if ext else ""
        logger.info(
            "    %s -> %s%s",
            m["alias"], m["accession"], ext_suffix,
        )
    logger.info("=" * 60)


if __name__ == "__main__":
    app()
