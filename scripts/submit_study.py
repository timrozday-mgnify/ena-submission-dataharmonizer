#!/usr/bin/env python3
"""Submit studies to ENA via the Webin REST API v2.

Read a JSON file containing study metadata, check for duplicate studies
already registered under the Webin account, construct an XML submission
document, and submit new studies to ENA.

Credentials are read from environment variables:

    export ENA_WEBIN=Webin-XXXXX
    export ENA_WEBIN_PASSWORD=SECRET

Usage::

    python scripts/submit_study.py --input studies.json --xsd assets/ena_schema --test

    # With hold date (max 2 years):
    python scripts/submit_study.py --input studies.json --xsd assets/ena_schema --hold-until 2028-01-01
"""

from __future__ import annotations

import json
import logging
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import httpx
import pendulum
import typer

import ena_common as common
from ena_api import WebinClient, WebinConfig

app = typer.Typer(help="Submit studies to ENA via the Webin REST API v2.")
logger = logging.getLogger("ena_submit.study")


# -------------------------------------------------------------------
# XML construction
# -------------------------------------------------------------------

def build_submission_xml(
    studies: list[dict[str, Any]],
    hold_until: str | None = None,
    action: str = "ADD",
) -> ET.Element:
    """Build a WEBIN XML document for submitting studies."""
    webin = ET.Element("WEBIN")

    submission_set = ET.SubElement(webin, "SUBMISSION_SET")
    submission = ET.SubElement(submission_set, "SUBMISSION")
    submission.set("alias", "study-submission-" + pendulum.now().format("YYYYMMDD-HHmmss"))
    actions = ET.SubElement(submission, "ACTIONS")
    ET.SubElement(ET.SubElement(actions, "ACTION"), action.upper())
    if hold_until:
        hold_el = ET.SubElement(ET.SubElement(actions, "ACTION"), "HOLD")
        hold_el.set("HoldUntilDate", hold_until)

    project_set = ET.SubElement(webin, "PROJECT_SET")
    for study in studies:
        _add_project_element(project_set, study)

    return webin


def _add_project_element(project_set: ET.Element, study: dict[str, Any]) -> None:
    alias = study.get("alias", study.get("STUDY_TITLE", "").replace(" ", "_")[:50])
    project = ET.SubElement(project_set, "PROJECT")
    project.set("alias", alias)

    name_text = study.get("CENTER_PROJECT_NAME", alias)
    if name_text:
        ET.SubElement(project, "NAME").text = name_text

    ET.SubElement(project, "TITLE").text = study.get("STUDY_TITLE", "")

    desc_text = study.get("STUDY_ABSTRACT") or study.get("STUDY_DESCRIPTION", "")
    if desc_text:
        ET.SubElement(project, "DESCRIPTION").text = desc_text

    ET.SubElement(ET.SubElement(project, "SUBMISSION_PROJECT"), "SEQUENCING_PROJECT")

    study_type = study.get("existing_study_type")
    if study_type:
        attrs = ET.SubElement(project, "PROJECT_ATTRIBUTES")
        _add_project_attribute(attrs, "existing_study_type", study_type)
        new_type = study.get("new_study_type")
        if new_type and study_type == "Other":
            _add_project_attribute(attrs, "new_study_type", new_type)


def _add_project_attribute(parent: ET.Element, tag_text: str, value_text: str) -> None:
    attr = ET.SubElement(parent, "PROJECT_ATTRIBUTE")
    ET.SubElement(attr, "TAG").text = tag_text
    ET.SubElement(attr, "VALUE").text = value_text


# -------------------------------------------------------------------
# XSD fallback structural checker
# -------------------------------------------------------------------

def _validate_study_xml_structure(xml_bytes: bytes, messages: list[str]) -> tuple[bool, list[str]]:
    """Fallback structural check for study XML."""
    try:
        tree = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        messages.append(f"ERROR: XML is not well-formed: {exc}")
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
            messages.append(f"ERROR: PROJECT '{alias}' missing TITLE")
            return False, messages
        sp = proj.find("SUBMISSION_PROJECT")
        if sp is None:
            messages.append(f"ERROR: PROJECT '{alias}' missing SUBMISSION_PROJECT")
            return False, messages
        messages.append(f"OK: PROJECT '{alias}' has required elements")

    return True, messages


# -------------------------------------------------------------------
# Public library functions
# -------------------------------------------------------------------

def build_manifest(
    studies: list[dict[str, Any]],
    *,
    hold_until: str | None = None,
    action: str = "ADD",
) -> bytes:
    """Build and serialise a WEBIN XML submission document for studies."""
    xml_root = build_submission_xml(studies, hold_until=hold_until, action=action)
    return common.xml_to_bytes(xml_root)


def validate_manifest(xml_bytes: bytes, xsd_dir: str | Path) -> tuple[bool, list[str]]:
    """Validate study XML against ENA.project.xsd."""
    return common.validate_xml_against_xsd(
        xml_bytes, xsd_dir,
        xsd_filename="ENA.project.xsd",
        fragment_tag="PROJECT_SET",
        fallback_checker=_validate_study_xml_structure,
    )


def submit_manifest(
    xml_bytes: bytes,
    client: WebinClient,
) -> tuple[bool, list[dict[str, str]], list[str]]:
    """Submit XML to ENA and parse the receipt.

    Args:
        xml_bytes: Serialised WEBIN XML document.
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


# -------------------------------------------------------------------
# Receipt parsing
# -------------------------------------------------------------------

def parse_xml_receipt(receipt_root: ET.Element) -> tuple[bool, list[dict[str, str]], list[str]]:
    """Parse an ENA XML receipt for study submissions."""
    success = receipt_root.get("success", "false").lower() == "true"
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
            "holdUntilDate": proj.get("holdUntilDate", ""),
        }
        ext = proj.find("EXT_ID")
        if ext is not None:
            acc_info["external_accession"] = ext.get("accession", "")
            acc_info["external_type"] = ext.get("type", "")
        accessions.append(acc_info)

    # Some receipts use STUDY instead of PROJECT.
    for study in receipt_root.findall("STUDY"):
        accessions.append({
            "alias": study.get("alias", ""),
            "accession": study.get("accession", ""),
            "status": study.get("status", ""),
        })

    return success, accessions, messages


# -------------------------------------------------------------------
# JSON loader
# -------------------------------------------------------------------

def _load_studies_json(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text())
    if isinstance(data, list):
        return data
    for key in ("studies", "data"):
        if key in data and isinstance(data[key], list):
            return data[key]
    raise ValueError(
        f"Cannot find studies list in JSON. Expected a top-level list or a dict with a "
        f"'studies' key. Got: {list(data.keys()) if isinstance(data, dict) else type(data).__name__}"
    )


# -------------------------------------------------------------------
# Full submission pipeline
# -------------------------------------------------------------------

def submit_studies(
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
    """Full study submission pipeline. Returns a results dict."""
    env_label = "TEST" if test else "PRODUCTION"
    username, password = common.get_credentials()
    client = WebinClient(config=WebinConfig(webin_id=username, password=password, test=test))

    if hold_until:
        common.validate_hold_until(hold_until)

    logger.info("Loading input: %s", input_file)
    studies = _load_studies_json(input_file)
    logger.info("Loaded %d study/studies from input", len(studies))

    if automated:
        logger.info("Automated mode: skipping duplicate detection")
        duplicates: dict[int, dict[str, Any]] = {}
    else:
        logger.info("Fetching account studies via Webin Reports API...")
        reports = client.reports.list_projects(max_results=max_results)
        logger.info("Found %d studies in account", len(reports))
        for r in reports:
            logger.info("  Account study: %s | alias=%s | title=%s | status=%s",
                        r.accession, r.alias, r.title, r.status)
        duplicates = common.find_duplicates_by_alias_title(
            studies, [r.model_dump() for r in reports],
            title_field="STUDY_TITLE", entity_label="studies",
        )

    results: dict[str, list] = {"duplicates": [], "submitted": [], "modified": [], "failed": []}

    studies_to_modify: list[dict[str, Any]] = []
    for idx, dup_info in duplicates.items():
        study_title = studies[idx].get("STUDY_TITLE", f"study[{idx}]")
        action_label = "will be re-submitted with MODIFY" if force else "will NOT be submitted"
        logger.warning("DUPLICATE: '%s' matches existing %s (accession: %s) — %s",
                       study_title, dup_info["match_reason"], dup_info["accession"], action_label)
        results["duplicates"].append({
            "input_index": idx,
            "title": study_title,
            "alias": studies[idx].get("alias", ""),
            "existing_accession": dup_info["accession"],
            "existing_secondary_accession": dup_info.get("secondary_accession", ""),
            "match_reason": dup_info["match_reason"],
        })
        if force:
            study_copy = dict(studies[idx])
            existing_alias = dup_info.get("alias", "")
            if existing_alias:
                study_copy["alias"] = existing_alias
            studies_to_modify.append(study_copy)

    studies_to_submit = [s for i, s in enumerate(studies) if i not in duplicates]

    if not studies_to_submit and not studies_to_modify:
        logger.info("No studies to submit (all are duplicates or input is empty)")
        return results

    logger.info("%d new study/studies to ADD, %d duplicate(s) to MODIFY",
                len(studies_to_submit), len(studies_to_modify))

    for batch, action, result_key in [
        (studies_to_submit, "ADD", "submitted"),
        (studies_to_modify, "MODIFY", "modified"),
    ]:
        if not batch:
            continue
        xml_bytes = build_manifest(batch, hold_until=hold_until, action=action)
        is_valid, xsd_msgs = validate_manifest(xml_bytes, xsd)
        for msg in xsd_msgs:
            logger.info("  %s", msg)
        if not is_valid:
            raise ValueError(f"{action} XML failed XSD validation")
        if dry_run:
            logger.info("DRY RUN — skipping %s submission", action)
            continue
        logger.info("Submitting %s to ENA (%s)...", action, env_label)
        success, accessions, receipt_msgs = submit_manifest(xml_bytes, client)
        for msg in receipt_msgs:
            logger.info("  Receipt: %s", msg)
        if success:
            logger.info("%s SUCCESSFUL", action)
            for acc in accessions:
                ext = acc.get("external_accession", "")
                logger.info("  %s: alias=%s accession=%s status=%s%s",
                            action, acc["alias"], acc["accession"], acc["status"],
                            f" (study: {ext})" if ext else "")
            results[result_key].extend(accessions)
        else:
            logger.error("%s FAILED", action)
            results["failed"].extend(accessions)

    return results


# -------------------------------------------------------------------
# CLI
# -------------------------------------------------------------------

def _log_summary(results: dict[str, list]) -> None:
    logger.info("=" * 60)
    logger.info("SUBMISSION SUMMARY")
    skipped = len(results["duplicates"]) - len(results["modified"])
    logger.info("  Duplicates skipped: %d", skipped)
    for d in results["duplicates"]:
        logger.info("    %s -> %s", d["title"], d["existing_accession"])
    logger.info("  Newly submitted (ADD): %d", len(results["submitted"]))
    for s in results["submitted"]:
        ext = s.get("external_accession", "")
        logger.info("    %s -> %s%s", s["alias"], s["accession"], f" ({ext})" if ext else "")
    logger.info("  Modified (MODIFY): %d", len(results["modified"]))
    for m in results["modified"]:
        ext = m.get("external_accession", "")
        logger.info("    %s -> %s%s", m["alias"], m["accession"], f" ({ext})" if ext else "")
    logger.info("=" * 60)


@app.command()
def main(
    input_file: Path = typer.Option(..., "--input", exists=True, help="Path to study metadata JSON file"),
    xsd: Path = typer.Option(..., exists=True, file_okay=False, resolve_path=True, help="Directory containing ENA.project.xsd and SRA.common.xsd"),
    test: bool = typer.Option(False, "--test", help="Use the ENA test service (submissions are discarded daily)"),
    hold_until: str | None = typer.Option(None, "--hold-until", help="Hold studies private until this date (YYYY-MM-DD, max 2 years from now)"),
    log: Path | None = typer.Option(None, help="Path to log file"),
    output: Path | None = typer.Option(None, help="Path to write JSON accession results (default: stdout)"),
    max_results: int = typer.Option(5000, "--max-results", help="Maximum number of projects to fetch from the Reports API for duplicate checking"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Validate and build XML but do not submit to ENA"),
    automated: bool = typer.Option(False, "--automated", help="Skip duplicate detection against the Webin Reports API (for automated pipelines)"),
    force: bool = typer.Option(False, "--force", help="Submit duplicate studies using the MODIFY action to overwrite existing ENA records"),
) -> None:
    """Submit studies to ENA via the Webin REST API v2."""
    common.setup_logging(log)
    env_label = "TEST" if test else "PRODUCTION"
    logger.info("ENA Study Submission — environment: %s", env_label)
    try:
        results = submit_studies(
            input_file, xsd,
            test=test, hold_until=hold_until, max_results=max_results,
            dry_run=dry_run, automated=automated, force=force,
        )
    except (ValueError, httpx.HTTPStatusError) as exc:
        logger.error("%s", exc)
        raise typer.Exit(1)
    common.write_results(results, output)
    _log_summary(results)


if __name__ == "__main__":
    app()
