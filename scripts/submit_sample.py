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
) -> ET.Element:
    """Build a WEBIN XML document for submitting samples.

    Each sample in the input list is converted to a SAMPLE
    element.  Fields not consumed by dedicated XML elements
    are emitted as ``<SAMPLE_ATTRIBUTE>`` tag-value pairs.

    Args:
        samples: Sample metadata dicts.
        hold_until: Optional hold-until date string
            (``YYYY-MM-DD``).

    Returns:
        Root ``<WEBIN>`` element.
    """
    webin = ET.Element("WEBIN")

    submission_set = ET.SubElement(webin, "SUBMISSION_SET")
    submission = ET.SubElement(
        submission_set, "SUBMISSION",
    )
    alias = (
        "sample-submission-"
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

    sample_set = ET.SubElement(webin, "SAMPLE_SET")
    for sample in samples:
        _add_sample_element(sample_set, sample)

    return webin


def _add_sample_element(
    sample_set: ET.Element,
    sample: dict[str, Any],
) -> None:
    """Append a ``<SAMPLE>`` element to *sample_set*."""
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
    attrs = {
        k: v for k, v in sample.items()
        if k not in _RESERVED_FIELDS
        and v is not None
        and str(v).strip()
    }
    if attrs:
        attrs_el = ET.SubElement(
            sample_el, "SAMPLE_ATTRIBUTES",
        )
        for tag, value in attrs.items():
            _add_sample_attribute(
                attrs_el, tag, str(value),
            )


def _add_sample_attribute(
    parent: ET.Element,
    tag_text: str,
    value_text: str,
) -> None:
    """Append a ``<SAMPLE_ATTRIBUTE>`` to *parent*."""
    attr = ET.SubElement(parent, "SAMPLE_ATTRIBUTE")
    tag_el = ET.SubElement(attr, "TAG")
    tag_el.text = tag_text
    val_el = ET.SubElement(attr, "VALUE")
    val_el.text = value_text


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
        "failed": [],
    }

    if duplicates:
        logger.warning(
            "Found %d duplicate(s)"
            " — these will NOT be submitted:",
            len(duplicates),
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

    samples_to_submit = [
        s for i, s in enumerate(samples)
        if i not in duplicates
    ]

    if not samples_to_submit:
        logger.info(
            "No new samples to submit"
            " (all are duplicates or input is empty)",
        )
        common.write_results(results, output)
        return

    logger.info(
        "%d sample(s) to submit after duplicate check",
        len(samples_to_submit),
    )

    # -- Step 3: Validate against LinkML -----------------
    logger.info("Loading LinkML schema: %s", linkml)
    schema = common.load_linkml_schema(linkml)

    logger.info(
        "Validating input against LinkML schema...",
    )
    linkml_valid, linkml_messages = (
        common.validate_against_linkml(
            samples_to_submit, schema,
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

    # -- Step 4: Build submission XML --------------------
    logger.info("Building XML submission document...")
    xml_root = build_submission_xml(
        samples_to_submit, hold_until=hold_until,
    )
    xml_bytes = common.xml_to_bytes(xml_root)

    logger.debug(
        "Generated XML:\n%s",
        xml_bytes.decode("utf-8"),
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
            "XSD validation FAILED"
            " — aborting submission",
        )
        sys.exit(1)

    logger.info("XSD validation PASSED")

    # -- Step 6: Submit to ENA ---------------------------
    if dry_run:
        logger.info(
            "DRY RUN — skipping actual submission",
        )
        logger.info(
            "Generated XML:\n%s",
            xml_bytes.decode("utf-8"),
        )
        common.write_results(results, output)
        return

    logger.info("Submitting to ENA (%s)...", env_label)
    try:
        receipt_root = common.submit_xml(
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
            ext_suffix = (
                f" (biosample: {ext})" if ext else ""
            )
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
    common.write_results(results, output)

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


if __name__ == "__main__":
    app()
