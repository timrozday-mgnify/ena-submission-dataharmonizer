#!/usr/bin/env python3
"""Submit samples to ENA via the Webin REST API v2.

Read a JSON file containing sample metadata, validate against an XSD schema,
check for duplicates, and submit to ENA.

Credentials are read from environment variables::

    export ENA_WEBIN=Webin-XXXXX
    export ENA_WEBIN_PASSWORD=SECRET

Usage::

    python scripts/submit_sample.py --input samples.json --xsd assets/ena_schema --test
    python scripts/submit_sample.py --input samples.json --xsd assets/ena_schema --dry-run

Library usage::

    from scripts.submit_sample import build_manifest, validate_manifest, submit_manifest, submit_samples

    xml_bytes = build_manifest(samples)
    is_valid, messages = validate_manifest(xml_bytes, xsd_dir)
    success, accessions, messages = submit_manifest(xml_bytes, client)

    # Or all-in-one:
    results = submit_samples(Path("samples.json"), Path("assets/ena_schema"))
"""

from __future__ import annotations

import json
import logging
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Final

import httpx
import pendulum
import typer

import ena_common as common
from ena_api import WebinClient, WebinConfig


app = typer.Typer(help="Submit samples to ENA via the Webin REST API v2.")
logger = logging.getLogger("ena_submit.sample")

# Fields consumed as dedicated XML elements, not SAMPLE_ATTRIBUTE tag-value pairs.
_RESERVED_FIELDS: Final = frozenset({
    "alias", "SAMPLE_TITLE", "TAXON_ID", "SCIENTIFIC_NAME",
    "COMMON_NAME", "SAMPLE_DESCRIPTION", "SAMPLE_ABSTRACT",
})

# ---------------------------------------------------------------------------
# Reports API
# ---------------------------------------------------------------------------

def fetch_account_samples(client: WebinClient, max_results: int = 5000) -> list[dict[str, str]]:
    """Fetch all samples registered under the Webin account via the Reports API."""
    logger.info("Fetching account samples via Webin Reports API...")
    reports = client.reports.list_samples(max_results=max_results)
    logger.info("Found %d samples in account", len(reports))
    return [
        {
            "alias": r.alias,
            "title": r.title,
            "accession": r.accession,
            "secondary_accession": r.secondary_accession,
            "status": r.status,
        }
        for r in reports
    ]


def find_duplicate_samples(
    new_samples: list[dict[str, Any]], account_samples: list[dict[str, str]],
) -> dict[int, dict[str, str]]:
    """Return a mapping of input index → existing account sample for each duplicate."""
    return common.find_duplicates_by_alias_title(
        new_samples, account_samples, title_field="SAMPLE_TITLE", entity_label="samples",
    )


# ---------------------------------------------------------------------------
# XML construction
# ---------------------------------------------------------------------------

def build_submission_xml(
    samples: list[dict[str, Any]],
    hold_until: str | None = None,
    checklist_id: str | None = None,
    slot_to_title: dict[str, str] | None = None,
    slot_to_unit: dict[str, str] | None = None,
    action: str = "ADD",
) -> ET.Element:
    """Build a WEBIN XML document for the given samples.

    Fields not in _RESERVED_FIELDS become SAMPLE_ATTRIBUTE tag-value pairs.
    slot_to_title maps slot names to human-readable tag names; slot_to_unit adds UNITS elements.
    """
    webin = ET.Element("WEBIN")

    submission = ET.SubElement(ET.SubElement(webin, "SUBMISSION_SET"), "SUBMISSION")
    submission.set("alias", "sample-submission-" + pendulum.now().format("YYYYMMDD-HHmmss"))
    actions = ET.SubElement(submission, "ACTIONS")
    ET.SubElement(ET.SubElement(actions, "ACTION"), action.upper())
    if hold_until:
        ET.SubElement(ET.SubElement(actions, "ACTION"), "HOLD").set("HoldUntilDate", hold_until)

    sample_set = ET.SubElement(webin, "SAMPLE_SET")
    for sample in samples:
        _add_sample_element(sample_set, sample, checklist_id, slot_to_title, slot_to_unit)

    return webin


def _add_sample_element(
    sample_set: ET.Element,
    sample: dict[str, Any],
    checklist_id: str | None = None,
    slot_to_title: dict[str, str] | None = None,
    slot_to_unit: dict[str, str] | None = None,
) -> None:
    alias = sample.get("alias") or (sample.get("SAMPLE_TITLE", "") or "").replace(" ", "_")[:50]
    sample_el = ET.SubElement(sample_set, "SAMPLE")
    sample_el.set("alias", alias)

    if title := sample.get("SAMPLE_TITLE", ""):
        ET.SubElement(sample_el, "TITLE").text = title

    sample_name = ET.SubElement(sample_el, "SAMPLE_NAME")
    ET.SubElement(sample_name, "TAXON_ID").text = str(sample.get("TAXON_ID", ""))
    if sci_name := sample.get("SCIENTIFIC_NAME", ""):
        ET.SubElement(sample_name, "SCIENTIFIC_NAME").text = sci_name
    if common_name := sample.get("COMMON_NAME", ""):
        ET.SubElement(sample_name, "COMMON_NAME").text = common_name

    if desc := sample.get("SAMPLE_DESCRIPTION") or sample.get("SAMPLE_ABSTRACT", ""):
        ET.SubElement(sample_el, "DESCRIPTION").text = desc

    attrs = {k: v for k, v in sample.items() if k not in _RESERVED_FIELDS and v is not None and str(v).strip()}
    if attrs or checklist_id:
        attrs_el = ET.SubElement(sample_el, "SAMPLE_ATTRIBUTES")
        if checklist_id:
            _add_sample_attribute(attrs_el, "ENA-CHECKLIST", checklist_id)
        for tag, value in attrs.items():
            tag_name = (slot_to_title or {}).get(tag, tag)
            _add_sample_attribute(attrs_el, tag_name, str(value), unit=(slot_to_unit or {}).get(tag))


def _add_sample_attribute(
    parent: ET.Element, tag_text: str, value_text: str, unit: str | None = None,
) -> None:
    attr = ET.SubElement(parent, "SAMPLE_ATTRIBUTE")
    ET.SubElement(attr, "TAG").text = tag_text
    ET.SubElement(attr, "VALUE").text = value_text
    if unit:
        ET.SubElement(attr, "UNITS").text = unit


# ---------------------------------------------------------------------------
# XSD validation
# ---------------------------------------------------------------------------

def _validate_sample_xml_structure(xml_bytes: bytes, messages: list[str]) -> tuple[bool, list[str]]:
    """Fallback structural check when lxml XSD validation is unavailable."""
    try:
        tree = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        messages.append(f"ERROR: XML is not well-formed: {exc}")
        return False, messages

    messages.append("XML is well-formed (basic check passed)")
    sample_set = tree.find("SAMPLE_SET")
    if sample_set is None:
        messages.append("ERROR: Missing SAMPLE_SET element")
        return False, messages
    samples = sample_set.findall("SAMPLE")
    if not samples:
        messages.append("ERROR: No SAMPLE elements found")
        return False, messages

    for sample in samples:
        alias = sample.get("alias", "<no alias>")
        sample_name = sample.find("SAMPLE_NAME")
        if sample_name is None:
            messages.append(f"ERROR: SAMPLE '{alias}' missing SAMPLE_NAME")
            return False, messages
        taxon = sample_name.find("TAXON_ID")
        if taxon is None or not taxon.text:
            messages.append(f"ERROR: SAMPLE '{alias}' missing TAXON_ID")
            return False, messages
        messages.append(f"OK: SAMPLE '{alias}' has required elements")

    return True, messages


def validate_against_xsd(xml_bytes: bytes, xsd_dir: str | Path) -> tuple[bool, list[str]]:
    """Validate sample XML against SRA.sample.xsd (with structural fallback)."""
    return common.validate_xml_against_xsd(
        xml_bytes, xsd_dir,
        xsd_filename="SRA.sample.xsd", fragment_tag="SAMPLE_SET",
        fallback_checker=_validate_sample_xml_structure,
    )


# ---------------------------------------------------------------------------
# Receipt parsing
# ---------------------------------------------------------------------------

def parse_xml_receipt(receipt_root: ET.Element) -> tuple[bool, list[dict[str, str]], list[str]]:
    """Parse an ENA XML receipt for sample submissions.

    Returns (success, accessions, messages).
    """
    success = receipt_root.get("success", "false").lower() == "true"
    accessions: list[dict[str, str]] = []
    messages: list[str] = []

    if (msgs_el := receipt_root.find("MESSAGES")) is not None:
        for info in msgs_el.findall("INFO"):
            messages.append(f"INFO: {info.text}")
        for err in msgs_el.findall("ERROR"):
            messages.append(f"ERROR: {err.text}")

    for sample in receipt_root.findall("SAMPLE"):
        acc: dict[str, str] = {
            "alias": sample.get("alias", ""),
            "accession": sample.get("accession", ""),
            "status": sample.get("status", ""),
            "holdUntilDate": sample.get("holdUntilDate", ""),
        }
        if (ext := sample.find("EXT_ID")) is not None:
            acc["external_accession"] = ext.get("accession", "")
            acc["external_type"] = ext.get("type", "")
        accessions.append(acc)

    return success, accessions, messages


# ---------------------------------------------------------------------------
# Public library API
# ---------------------------------------------------------------------------

def build_manifest(
    samples: list[dict[str, Any]],
    *,
    hold_until: str | None = None,
    action: str = "ADD",
) -> bytes:
    """Build an ENA sample XML submission document.

    Args:
        samples: List of sample metadata dicts (keys are field names).
        hold_until: Optional hold-until date (YYYY-MM-DD).
        action: "ADD" for new samples or "MODIFY" to update existing ones.

    Returns:
        Serialised XML bytes ready for validate_manifest() or submit_manifest().
    """
    xml_root = build_submission_xml(samples, hold_until=hold_until, action=action)
    return common.xml_to_bytes(xml_root)


def validate_manifest(xml_bytes: bytes, xsd_dir: Path) -> tuple[bool, list[str]]:
    """Validate an ENA sample XML manifest against SRA.sample.xsd.

    Returns (is_valid, messages).
    """
    return validate_against_xsd(xml_bytes, xsd_dir)


def submit_manifest(
    xml_bytes: bytes,
    client: WebinClient,
) -> tuple[bool, list[dict[str, str]], list[str]]:
    """Submit an ENA sample XML manifest and parse the receipt.

    Args:
        xml_bytes: Serialised XML submission document.
        client: Authenticated WebinClient instance.

    Returns:
        Tuple of (success, accessions, messages).

    Raises:
        httpx.HTTPStatusError: On HTTP failure.
    """
    receipt = client.submit.xml(xml_bytes)
    accessions = [
        {
            "alias": r.alias,
            "accession": r.accession,
            "status": r.status,
            "holdUntilDate": r.hold_until_date,
            "external_accession": r.external_accession,
            "external_type": r.external_type,
        }
        for r in receipt.accessions
    ]
    return receipt.success, accessions, receipt.messages + receipt.errors


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def _load_samples_json(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text())
    if isinstance(data, list):
        return data
    for key in ("samples", "data"):
        if key in data and isinstance(data[key], list):
            return data[key]
    raise ValueError(f"JSON must be a list or contain a 'samples' key; got keys: {list(data.keys())}")


def submit_samples(
    input_file: Path,
    xsd: Path,
    *,
    test: bool = False,
    hold_until: str | None = None,
    max_results: int = 5000,
    dry_run: bool = False,
    automated: bool = False,
    force: bool = False,
) -> dict[str, list]:
    """Load, validate, and submit samples to ENA.

    Args:
        input_file: Path to a JSON file containing sample metadata.
        xsd: Directory containing SRA.sample.xsd and SRA.common.xsd.
        test: Use the ENA test service.
        hold_until: Hold samples private until this date (YYYY-MM-DD, max 2 years).
        max_results: Max samples to fetch from the Reports API for duplicate checking.
        dry_run: Build and validate XML but do not submit.
        automated: Skip duplicate detection (for pipeline use).
        force: Re-submit duplicates using MODIFY instead of skipping.

    Returns:
        Results dict with keys: submitted, modified, duplicates, failed.

    Raises:
        ValueError: On invalid input or failed validation.
        httpx.HTTPStatusError: On HTTP submission failure.
    """
    username, password = common.get_credentials()
    client = WebinClient(config=WebinConfig(webin_id=username, password=password, test=test))
    env_label = "TEST" if test else "PRODUCTION"

    if hold_until:
        common.validate_hold_until(hold_until)

    logger.info("Loading input: %s", input_file)
    samples = _load_samples_json(input_file)
    logger.info("Loaded %d sample(s)", len(samples))

    duplicates: dict[int, dict[str, Any]] = {}
    if automated:
        logger.info("Automated mode: skipping duplicate detection")
    else:
        account_samples = fetch_account_samples(client, max_results=max_results)
        duplicates = find_duplicate_samples(samples, account_samples)

    results: dict[str, list] = {"duplicates": [], "submitted": [], "modified": [], "failed": []}

    samples_to_modify: list[dict[str, Any]] = []
    for idx, dup_info in duplicates.items():
        sample_title = samples[idx].get("SAMPLE_TITLE", f"sample[{idx}]")
        logger.warning("DUPLICATE '%s' matches existing %s (accession: %s)",
                       sample_title, dup_info["match_reason"], dup_info["accession"])
        results["duplicates"].append({
            "input_index": idx,
            "title": sample_title,
            "alias": samples[idx].get("alias", ""),
            "existing_accession": dup_info["accession"],
            "existing_secondary_accession": dup_info.get("secondary_accession", ""),
            "match_reason": dup_info["match_reason"],
        })
        if force:
            sample_copy = dict(samples[idx])
            if existing_alias := dup_info.get("alias"):
                sample_copy["alias"] = existing_alias
            samples_to_modify.append(sample_copy)

    samples_to_submit = [s for i, s in enumerate(samples) if i not in duplicates]

    if not samples_to_submit and not samples_to_modify:
        logger.info("No samples to submit (all duplicates or empty input)")
        return results

    logger.info("%d to ADD, %d to MODIFY", len(samples_to_submit), len(samples_to_modify))

    for batch, action, result_key in [
        (samples_to_submit, "ADD", "submitted"),
        (samples_to_modify, "MODIFY", "modified"),
    ]:
        if not batch:
            continue
        xml_bytes = build_manifest(batch, hold_until=hold_until, action=action)
        is_valid, xsd_messages = validate_manifest(xml_bytes, xsd)
        for msg in xsd_messages:
            logger.info("  %s", msg)
        if not is_valid:
            raise ValueError(f"{action} XML failed XSD validation")
        if dry_run:
            logger.info("DRY RUN — skipping %s (%s)", action, env_label)
            continue
        logger.info("Submitting %s to ENA (%s)...", action, env_label)
        success, accessions, receipt_messages = submit_manifest(xml_bytes, client)
        for msg in receipt_messages:
            logger.info("  Receipt: %s", msg)
        if success:
            logger.info("%s successful: %d sample(s)", action, len(accessions))
            results[result_key].extend(accessions)
        else:
            logger.error("%s failed", action)
            results["failed"].extend(accessions)

    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@app.command()
def main(
    input_file: Path = typer.Option(..., "--input", exists=True, help="Path to sample metadata JSON file"),
    xsd: Path = typer.Option(..., exists=True, file_okay=False, resolve_path=True, help="Directory containing SRA.sample.xsd and SRA.common.xsd"),
    test: bool = typer.Option(False, "--test", help="Use the ENA test service (submissions discarded daily)"),
    hold_until: str | None = typer.Option(None, "--hold-until", help="Hold samples private until this date (YYYY-MM-DD, max 2 years)"),
    log: Path | None = typer.Option(None, help="Path to log file"),
    output: Path | None = typer.Option(None, help="Path to write JSON results (default: stdout)"),
    max_results: int = typer.Option(5000, "--max-results", help="Max samples to fetch from Reports API for duplicate checking"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Validate and build XML but do not submit"),
    automated: bool = typer.Option(False, "--automated", help="Skip duplicate detection (for automated pipelines)"),
    force: bool = typer.Option(False, "--force", help="Re-submit duplicates with MODIFY instead of skipping"),
) -> None:
    """Submit samples to ENA via the Webin REST API v2."""
    common.setup_logging(log)
    logger.info("ENA Sample Submission — environment: %s", "TEST" if test else "PRODUCTION")
    try:
        results = submit_samples(
            input_file, xsd,
            test=test, hold_until=hold_until, max_results=max_results,
            dry_run=dry_run, automated=automated, force=force,
        )
    except (ValueError, httpx.HTTPStatusError) as exc:
        logger.error("%s", exc)
        raise typer.Exit(1)

    common.write_results(results, output)
    _log_summary(results)


def _log_summary(results: dict[str, list]) -> None:
    skipped = len(results["duplicates"]) - len(results["modified"])
    logger.info("=" * 60)
    logger.info("SUBMISSION SUMMARY")
    logger.info("  Duplicates skipped:  %d", skipped)
    for d in results["duplicates"]:
        logger.info("    %s -> %s", d["title"], d["existing_accession"])
    logger.info("  Submitted (ADD):     %d", len(results["submitted"]))
    for s in results["submitted"]:
        ext = s.get("external_accession", "")
        logger.info("    %s -> %s%s", s["alias"], s["accession"], f" ({ext})" if ext else "")
    logger.info("  Modified (MODIFY):   %d", len(results["modified"]))
    for m in results["modified"]:
        ext = m.get("external_accession", "")
        logger.info("    %s -> %s%s", m["alias"], m["accession"], f" ({ext})" if ext else "")
    logger.info("=" * 60)


if __name__ == "__main__":
    app()
