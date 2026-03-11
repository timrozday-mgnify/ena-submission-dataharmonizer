#!/usr/bin/env python3
"""Submit reads to ENA via webin-cli (reads context).

Read a DataHarmonizer export containing run/experiment metadata,
validate it against a LinkML schema, check for duplicates already
registered under the Webin account, generate webin-cli manifest
files, and submit each run to ENA.

Credentials are read from environment variables to avoid secrets
appearing in shell history or process listings::

    export ENA_USERNAME=Webin-XXXXX
    export ENA_PASSWORD=SECRET

One row in the input file corresponds to one run submission.
Required input fields (not necessarily in the LinkML schema):

    STUDY         Study accession or alias (e.g. PRJEB12345)
    SAMPLE        Sample accession or alias (e.g. SAMEA7687881)
    NAME          Unique run name / alias within the account
    INSTRUMENT    Sequencing instrument model (e.g. "Illumina MiSeq")
    LIBRARY_SOURCE, LIBRARY_SELECTION, LIBRARY_STRATEGY

At least one of the following file fields must be present:

    FASTQ         Path to a single FASTQ file
    FASTQ1        Path to read 1 (paired); FASTQ2 for read 2
    BAM           Path to a BAM file
    CRAM          Path to a CRAM file

Usage::

    python scripts/submit_reads.py \\
        --input runs.csv \\
        --linkml schemas/SRA_experiment.yaml \\
        --xsd assets/ena_schema \\
        --test

    # Without submitting (just validate):
    python scripts/submit_reads.py \\
        --input runs.json \\
        --linkml schemas/SRA_experiment.yaml \\
        --xsd assets/ena_schema \\
        --dry-run --log reads.log

    # With a pre-downloaded webin-cli jar:
    python scripts/submit_reads.py \\
        --input runs.csv \\
        --linkml schemas/SRA_experiment.yaml \\
        --xsd assets/ena_schema \\
        --webin-cli-jar /path/to/webin-cli.jar

    # Download webin-cli automatically:
    python scripts/submit_reads.py \\
        --input runs.csv \\
        --linkml schemas/SRA_experiment.yaml \\
        --xsd assets/ena_schema \\
        --download-webin-cli
"""

import csv
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from io import BytesIO
from pathlib import Path
from typing import Any, Final

import lxml.etree

import requests
import typer
import yaml
from requests.auth import HTTPBasicAuth

app = typer.Typer(
    help="Submit reads to ENA via webin-cli (reads context).",
)

logger = logging.getLogger("submit_reads")

# Password env var passed to webin-cli via -passwordEnv.
# Webin-cli reads the password from this env var at runtime,
# so the secret never appears in the process command line.
_PASSWORD_ENV: Final = "ENA_PASSWORD"

# Retry defaults (from upstream webin_cli_handler.py)
_DEFAULT_RETRIES: Final = 3
_DEFAULT_RETRY_DELAY: Final = 5  # seconds
_DEFAULT_HEAP_INITIAL: Final = 10  # GB
_DEFAULT_HEAP_MAX: Final = 10  # GB

PROD_REPORTS_URL: Final = (
    "https://www.ebi.ac.uk/ena/submit/report/runs"
)
TEST_REPORTS_URL: Final = (
    "https://wwwdev.ebi.ac.uk/ena/submit/report/runs"
)
WEBIN_CLI_RELEASES_URL: Final = (
    "https://api.github.com/repos/enasequence/webin-cli"
    "/releases/latest"
)


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
# Webin-CLI management
# -----------------------------------------------------------


def get_latest_webin_cli_version() -> str:
    """Return the latest webin-cli release tag from GitHub.

    Returns:
        Version string, e.g. ``'9.0.1'``.

    Raises:
        typer.Exit: If the GitHub API is unreachable.
    """
    try:
        with urllib.request.urlopen(
            WEBIN_CLI_RELEASES_URL, timeout=10,
        ) as resp:
            data = json.load(resp)
            return data["tag_name"]
    except (
        urllib.error.URLError,
        KeyError,
        json.JSONDecodeError,
    ) as exc:
        logger.error(
            "Failed to fetch latest webin-cli version: %s",
            exc,
        )
        raise typer.Exit(1) from exc


def _download_webin_cli(
    dest_dir: Path,
    version: str | None = None,
) -> Path:
    """Download the webin-cli jar to *dest_dir*.

    Args:
        dest_dir: Directory in which to save the jar.
        version: Specific version to download. If ``None``,
            the latest release is fetched automatically.

    Returns:
        Path to the downloaded jar file.

    Raises:
        typer.Exit: If the download fails.
    """
    if version is None:
        version = get_latest_webin_cli_version()

    jar_url = (
        f"https://github.com/enasequence/webin-cli"
        f"/releases/download/{version}"
        f"/webin-cli-{version}.jar"
    )
    dest_path = dest_dir / "webin-cli.jar"

    logger.info(
        "Downloading webin-cli %s to %s ...",
        version, dest_path,
    )
    try:
        urllib.request.urlretrieve(jar_url, str(dest_path))
    except (urllib.error.URLError, OSError) as exc:
        logger.error(
            "Failed to download webin-cli: %s", exc,
        )
        raise typer.Exit(1) from exc

    logger.info("Downloaded webin-cli jar: %s", dest_path)
    return dest_path


def find_webin_cli(
    jar_path: Path | None,
) -> tuple[str, list[str]]:
    """Locate the webin-cli executable or jar.

    Checks in order: provided jar → ``ena-webin-cli`` in PATH.

    Args:
        jar_path: Optional path to a webin-cli jar file.

    Returns:
        Tuple of (*label*, *cmd_prefix*). The label is used
        for logging; the cmd prefix is the start of the
        subprocess command list.

    Raises:
        typer.Exit: If neither a jar nor ``ena-webin-cli``
            can be found.
    """
    if jar_path is not None:
        logger.info("Using webin-cli jar: %s", jar_path)
        cmd_prefix = [
            "java",
            f"-Xms{_DEFAULT_HEAP_INITIAL}g",
            f"-Xmx{_DEFAULT_HEAP_MAX}g",
            "-jar", str(jar_path),
        ]
        return str(jar_path), cmd_prefix

    cli_path = shutil.which("ena-webin-cli")
    if cli_path:
        logger.info("Using ena-webin-cli from: %s", cli_path)
        return cli_path, ["ena-webin-cli"]

    logger.error(
        "ena-webin-cli not found in PATH and no --webin-cli-jar"
        " provided. Install with conda/mamba, or pass"
        " --webin-cli-jar / --download-webin-cli.",
    )
    raise typer.Exit(1)


# -----------------------------------------------------------
# ENA Reports API (duplicate detection)
# -----------------------------------------------------------


def fetch_account_runs(
    auth: HTTPBasicAuth,
    use_test: bool = False,
    max_results: int = 5000,
) -> list[dict[str, str]]:
    """Fetch all runs from the Webin Reports API.

    Args:
        auth: HTTP basic-auth credentials.
        use_test: Try the test endpoint before production.
        max_results: Maximum number of results to request.

    Returns:
        List of run dicts with keys: name, accession, status.
    """
    urls = (
        [TEST_REPORTS_URL, PROD_REPORTS_URL]
        if use_test
        else [PROD_REPORTS_URL]
    )

    for url in urls:
        logger.info("Fetching account runs from: %s", url)
        raw = _fetch_runs_from_reports(url, auth, max_results)
        if raw is None:
            continue

        runs: list[dict[str, str]] = []
        for entry in raw:
            r = entry.get("report")
            if r is None:
                continue
            runs.append({
                "name": (
                    r.get("alias")
                    or r.get("runAlias")
                    or ""
                ),
                "accession": (
                    r.get("accession")
                    or r.get("runAccession")
                    or ""
                ),
                "status": r.get("releaseStatus", "UNKNOWN"),
            })

        logger.info("Found %d runs in account", len(runs))
        return runs

    logger.warning(
        "Could not reach any Webin reports endpoint. "
        "Duplicate checking will be skipped.",
    )
    return []


def _fetch_runs_from_reports(
    url: str,
    auth: HTTPBasicAuth,
    max_results: int,
) -> list[dict[str, Any]] | None:
    """Fetch runs from a single reports endpoint.

    Returns:
        List of raw run dicts, or ``None`` on error.
    """
    params = {"format": "json", "max-results": max_results}
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
            return []
        if status in (401, 403):
            logger.warning(
                "Reports API at %s returned %s",
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


def find_duplicate_runs(
    new_runs: Sequence[dict[str, Any]],
    account_runs: Sequence[dict[str, str]],
) -> dict[int, dict[str, str]]:
    """Check new runs against existing account runs by name.

    Args:
        new_runs: Runs the user wants to submit.
        account_runs: Existing runs already registered.

    Returns:
        Mapping of index in *new_runs* to matching run info.
    """
    duplicates: dict[int, dict[str, str]] = {}
    if not account_runs:
        return duplicates

    by_name: dict[str, dict[str, str]] = {
        r["name"]: r
        for r in account_runs
        if r.get("name")
    }

    logger.info(
        "Checking %d new runs against %d existing...",
        len(new_runs), len(account_runs),
    )

    for i, run in enumerate(new_runs):
        name = _run_name(run).strip()
        if not name:
            continue
        if name in by_name:
            match = by_name[name]
            duplicates[i] = {
                "accession": match.get("accession", ""),
                "name": match.get("name", ""),
                "status": match.get("status", "UNKNOWN"),
                "match_reason": f"name '{name}'",
            }
            logger.info(
                "  Duplicate: '%s' -> %s (%s)",
                name, match.get("accession", ""),
                match.get("status", ""),
            )
            if len(duplicates) == len(new_runs):
                logger.info(
                    "All runs are duplicates"
                    " — skipping further checks",
                )
                return duplicates

    return duplicates


# -----------------------------------------------------------
# XML construction and XSD validation
# -----------------------------------------------------------

# Maps lowercase substrings of an INSTRUMENT field to the
# corresponding SRA platform element name.
_PLATFORM_KEYWORDS: Final[list[tuple[str, str]]] = [
    ("illumina", "ILLUMINA"),
    ("nextseq", "ILLUMINA"),
    ("novaseq", "ILLUMINA"),
    ("hiseq", "ILLUMINA"),
    ("miseq", "ILLUMINA"),
    ("miniseq", "ILLUMINA"),
    ("iseq", "ILLUMINA"),
    ("genome analyzer", "ILLUMINA"),
    ("oxford nanopore", "OXFORD_NANOPORE"),
    ("nanopore", "OXFORD_NANOPORE"),
    ("minion", "OXFORD_NANOPORE"),
    ("gridion", "OXFORD_NANOPORE"),
    ("promethion", "OXFORD_NANOPORE"),
    ("flongle", "OXFORD_NANOPORE"),
    ("pacbio", "PACBIO_SMRT"),
    ("sequel", "PACBIO_SMRT"),
    ("revio", "PACBIO_SMRT"),
    ("ion torrent", "ION_TORRENT"),
    ("ion pgm", "ION_TORRENT"),
    ("ion proton", "ION_TORRENT"),
    ("ion s5", "ION_TORRENT"),
    ("bgiseq", "BGISEQ"),
    ("dnbseq", "DNBSEQ"),
    ("mgiseq", "BGISEQ"),
    ("454", "LS454"),
    ("gs flx", "LS454"),
    ("solid", "ABI_SOLID"),
    ("helicos", "HELICOS"),
    ("capillary", "CAPILLARY"),
    ("abi prism", "CAPILLARY"),
    ("3730", "CAPILLARY"),
    ("complete genomics", "COMPLETE_GENOMICS"),
    ("ultima", "ULTIMA"),
    ("element biosciences", "ELEMENT"),
    ("genapsys", "GENAPSYS"),
    ("genemind", "GENEMIND"),
    ("tapestri", "TAPESTRI"),
]


def _detect_platform(instrument: str) -> str | None:
    """Return the SRA platform element name for *instrument*.

    Args:
        instrument: Free-text instrument model string.

    Returns:
        Platform element name (e.g. ``'ILLUMINA'``), or
        ``None`` if no keyword matched.
    """
    lower = instrument.lower()
    for keyword, platform in _PLATFORM_KEYWORDS:
        if keyword in lower:
            return platform
    return None


def _is_paired(run: dict[str, Any]) -> bool:
    """Return True if the run has a second FASTQ file."""
    return bool((run.get("FASTQ2") or "").strip())


def build_experiment_xml(
    runs: Sequence[dict[str, Any]],
) -> ET.Element:
    """Build an ``EXPERIMENT_SET`` XML element from *runs*.

    Each run dict produces one ``<EXPERIMENT>`` element.
    LIBRARY_LAYOUT is inferred from the presence of FASTQ2
    if not explicitly set in the input.

    Args:
        runs: Run metadata dicts.

    Returns:
        Root ``<EXPERIMENT_SET>`` element.
    """
    experiment_set = ET.Element("EXPERIMENT_SET")

    for run in runs:
        alias = _run_name(run)
        exp = ET.SubElement(experiment_set, "EXPERIMENT")
        exp.set("alias", alias)

        title = (
            run.get("TITLE") or run.get("NAME") or alias
        )
        title_el = ET.SubElement(exp, "TITLE")
        title_el.text = title

        # STUDY_REF
        study = (
            run.get("STUDY") or run.get("STUDY_REF") or ""
        ).strip()
        study_ref = ET.SubElement(exp, "STUDY_REF")
        if study.startswith(("PRJ", "ERP", "SRP", "DRP")):
            study_ref.set("accession", study)
        else:
            study_ref.set("refname", study)

        # DESIGN
        design = ET.SubElement(exp, "DESIGN")

        desc_el = ET.SubElement(design, "DESIGN_DESCRIPTION")
        desc_el.text = (
            run.get("DESIGN_DESCRIPTION") or ""
        )

        sample = (
            run.get("SAMPLE") or run.get("SAMPLE_REF") or ""
        ).strip()
        sample_desc = ET.SubElement(design, "SAMPLE_DESCRIPTOR")
        if sample.startswith(("SAMEA", "SAMD", "SAMN", "ERS", "SRS", "DRS")):
            sample_desc.set("accession", sample)
        else:
            sample_desc.set("refname", sample)

        # LIBRARY_DESCRIPTOR
        lib_desc = ET.SubElement(design, "LIBRARY_DESCRIPTOR")

        lib_name = (run.get("LIBRARY_NAME") or "").strip()
        if lib_name:
            lib_name_el = ET.SubElement(lib_desc, "LIBRARY_NAME")
            lib_name_el.text = lib_name

        lib_strategy_el = ET.SubElement(
            lib_desc, "LIBRARY_STRATEGY",
        )
        lib_strategy_el.text = (
            run.get("LIBRARY_STRATEGY") or ""
        )

        lib_source_el = ET.SubElement(
            lib_desc, "LIBRARY_SOURCE",
        )
        lib_source_el.text = (
            run.get("LIBRARY_SOURCE") or ""
        )

        lib_selection_el = ET.SubElement(
            lib_desc, "LIBRARY_SELECTION",
        )
        lib_selection_el.text = (
            run.get("LIBRARY_SELECTION") or ""
        )

        lib_layout = ET.SubElement(lib_desc, "LIBRARY_LAYOUT")
        layout_val = (
            run.get("LIBRARY_LAYOUT") or ""
        ).upper().strip()
        if layout_val == "PAIRED" or (
            not layout_val and _is_paired(run)
        ):
            paired = ET.SubElement(lib_layout, "PAIRED")
            nominal = (
                run.get("NOMINAL_LENGTH")
                or run.get("INSERT_SIZE")
                or ""
            )
            if nominal:
                paired.set("NOMINAL_LENGTH", str(nominal))
        else:
            ET.SubElement(lib_layout, "SINGLE")

        protocol = (
            run.get("LIBRARY_CONSTRUCTION_PROTOCOL") or ""
        ).strip()
        if protocol:
            proto_el = ET.SubElement(
                lib_desc, "LIBRARY_CONSTRUCTION_PROTOCOL",
            )
            proto_el.text = protocol

        # PLATFORM
        instrument = (run.get("INSTRUMENT") or "").strip()
        platform_el = ET.SubElement(exp, "PLATFORM")
        platform_name = _detect_platform(instrument) or "ILLUMINA"
        plat_inner = ET.SubElement(platform_el, platform_name)
        instr_model_el = ET.SubElement(
            plat_inner, "INSTRUMENT_MODEL",
        )
        instr_model_el.text = instrument

    return experiment_set


def _xml_to_bytes(root: ET.Element) -> bytes:
    """Serialise an ElementTree element to bytes."""
    tree = ET.ElementTree(root)
    buf = BytesIO()
    tree.write(buf, encoding="UTF-8", xml_declaration=True)
    return buf.getvalue()


def validate_against_xsd(
    runs: Sequence[dict[str, Any]],
    xsd_dir: str | Path,
) -> tuple[bool, list[str]]:
    """Validate experiment XML against SRA.experiment.xsd.

    Builds an ``EXPERIMENT_SET`` XML from *runs* and
    validates it against the XSD.  Falls back to basic
    well-formedness checking if the schema cannot be loaded.

    Args:
        runs: Run metadata dicts to validate.
        xsd_dir: Directory containing ``SRA.experiment.xsd``
            and ``SRA.common.xsd``.

    Returns:
        Tuple of (*is_valid*, *messages*).
    """
    messages: list[str] = []
    xsd_root = Path(xsd_dir).resolve()
    xsd_file = xsd_root / "SRA.experiment.xsd"
    common_file = xsd_root / "SRA.common.xsd"

    if not xsd_file.is_file():
        messages.append(
            f"ERROR: SRA.experiment.xsd not found"
            f" in {xsd_root}"
        )
        return False, messages

    if not common_file.is_file():
        messages.append(
            f"WARNING: SRA.common.xsd not found"
            f" in {xsd_root}"
            " — full XSD validation may fail"
        )

    experiment_set = build_experiment_xml(runs)
    xml_bytes = _xml_to_bytes(experiment_set)
    logger.debug(
        "Generated EXPERIMENT_SET XML:\n%s",
        xml_bytes.decode("utf-8"),
    )

    with open(xsd_file, "rb") as fh:
        xsd_doc = lxml.etree.parse(
            fh, base_url=f"file://{xsd_root}/",
        )

    try:
        xsd_schema = lxml.etree.XMLSchema(xsd_doc)
        parsed = lxml.etree.fromstring(xml_bytes)

        if xsd_schema.validate(parsed):
            messages.append("XSD validation passed (lxml)")
            return True, messages

        for error in xsd_schema.error_log:
            messages.append(f"XSD ERROR: {error}")
        return False, messages

    except lxml.etree.XMLSchemaParseError as exc:
        messages.append(
            f"WARNING: Could not build XSD schema"
            f" (missing imports?): {exc}."
            " Falling back to basic well-formedness check."
        )

    # Fallback: check well-formedness and required elements
    try:
        parsed_fb = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        messages.append(
            f"ERROR: XML is not well-formed: {exc}"
        )
        return False, messages

    messages.append("XML is well-formed (basic check passed)")
    experiments = parsed_fb.findall("EXPERIMENT")
    if not experiments:
        messages.append("ERROR: No EXPERIMENT elements found")
        return False, messages

    for exp in experiments:
        alias = exp.get("alias", "<no alias>")
        for required in ("STUDY_REF", "DESIGN", "PLATFORM"):
            if exp.find(required) is None:
                messages.append(
                    f"ERROR: EXPERIMENT '{alias}'"
                    f" missing {required}"
                )
                return False, messages
        messages.append(
            f"OK: EXPERIMENT '{alias}' has required elements"
        )

    return True, messages


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
    runs: Sequence[dict[str, Any]],
    schema: dict[str, Any],
) -> tuple[bool, list[str]]:
    """Validate run dicts against a LinkML schema.

    Args:
        runs: Run dicts to validate.
        schema: Parsed LinkML schema dict.

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

    class_slot_names: list[str] = main_class.get("slots", [])
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
    for i, run in enumerate(runs):
        label = run.get("NAME") or run.get("alias", f"run[{i}]")
        messages.append(f"\n--- Validating run: {label} ---")

        for key in run:
            if key not in class_slot_names and key != "alias":
                messages.append(
                    f"  INFO: Field '{key}' not in LinkML"
                    " schema (will be passed to manifest)"
                )

        for req in required_slots:
            val = run.get(req)
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
                    f"  OK: Required field '{req}' = {val!r}"
                )

        for slot_name in class_slot_names:
            val = run.get(slot_name)
            if val is None:
                continue
            expected_range = slot_ranges.get(slot_name, "string")
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
                    f"  OK: Field '{slot_name}' = {val!r}"
                )

    return is_valid, messages


def validate_run_fields(
    runs: Sequence[dict[str, Any]],
) -> tuple[bool, list[str]]:
    """Check webin-cli–required fields not covered by LinkML.

    Verifies that each run has the manifest-required fields
    (STUDY, SAMPLE, NAME, INSTRUMENT) and at least one data
    file field whose path exists on disk.

    Args:
        runs: Run dicts to validate.

    Returns:
        Tuple of (*is_valid*, *messages*).
    """
    messages: list[str] = []
    is_valid = True

    _FILE_FIELDS = ("FASTQ", "FASTQ1", "BAM", "CRAM")

    for i, run in enumerate(runs):
        label = run.get("NAME") or run.get("alias", f"run[{i}]")
        messages.append(
            f"\n--- Checking manifest fields: {label} ---"
        )

        # Resolve STUDY / SAMPLE allowing aliased field names
        study = (
            run.get("STUDY") or run.get("STUDY_REF") or ""
        ).strip()
        sample = (
            run.get("SAMPLE") or run.get("SAMPLE_REF") or ""
        ).strip()
        name = _run_name(run).strip()
        instrument = (run.get("INSTRUMENT") or "").strip()

        for field, val in (
            ("STUDY", study),
            ("SAMPLE", sample),
            ("NAME", name),
            ("INSTRUMENT", instrument),
        ):
            if not val:
                messages.append(
                    f"  ERROR: Required manifest field"
                    f" '{field}' is missing or empty"
                )
                is_valid = False
            else:
                messages.append(
                    f"  OK: '{field}' = {val!r}"
                )

        # Check at least one data file is specified
        found_file = False
        for field in _FILE_FIELDS:
            path_str = (run.get(field) or "").strip()
            if not path_str:
                continue
            p = Path(path_str)
            if not p.exists():
                messages.append(
                    f"  ERROR: File for field '{field}'"
                    f" does not exist: {path_str}"
                )
                is_valid = False
            else:
                messages.append(
                    f"  OK: '{field}' = {path_str}"
                )
            found_file = True

        if not found_file:
            messages.append(
                "  ERROR: No data file specified."
                " Provide FASTQ, FASTQ1, BAM, or CRAM."
            )
            is_valid = False

    return is_valid, messages


# -----------------------------------------------------------
# Manifest generation
# -----------------------------------------------------------


def _run_name(run: dict[str, Any]) -> str:
    """Return the run name / alias for *run*."""
    return (
        run.get("NAME")
        or run.get("alias")
        or ""
    )


def build_manifest(
    run: dict[str, Any],
    manifest_path: Path,
) -> None:
    """Write a webin-cli reads manifest file for *run*.

    Args:
        run: Run metadata dict.
        manifest_path: Destination path for the manifest.
    """
    lines: list[tuple[str, str]] = []

    def _add(key: str, val: str | None) -> None:
        if val and str(val).strip():
            lines.append((key, str(val).strip()))

    _add(
        "STUDY",
        run.get("STUDY") or run.get("STUDY_REF"),
    )
    _add(
        "SAMPLE",
        run.get("SAMPLE") or run.get("SAMPLE_REF"),
    )
    _add("NAME", _run_name(run))
    _add("INSTRUMENT", run.get("INSTRUMENT"))
    _add("LIBRARY_SOURCE", run.get("LIBRARY_SOURCE"))
    _add("LIBRARY_SELECTION", run.get("LIBRARY_SELECTION"))
    _add("LIBRARY_STRATEGY", run.get("LIBRARY_STRATEGY"))
    _add("LIBRARY_NAME", run.get("LIBRARY_NAME"))
    _add(
        "LIBRARY_CONSTRUCTION_PROTOCOL",
        run.get("LIBRARY_CONSTRUCTION_PROTOCOL"),
    )
    insert = (
        run.get("INSERT_SIZE")
        or run.get("NOMINAL_LENGTH")
    )
    _add("INSERT_SIZE", insert)
    _add("DESCRIPTION", run.get("DESCRIPTION"))

    # Data file fields — FASTQ1/FASTQ2 emit two FASTQ lines
    for field in ("FASTQ", "FASTQ1", "FASTQ2"):
        val = (run.get(field) or "").strip()
        if val:
            lines.append(("FASTQ", val))
    for field in ("BAM", "CRAM"):
        val = (run.get(field) or "").strip()
        if val:
            lines.append((field, val))

    with open(manifest_path, "w", encoding="utf-8") as fh:
        for key, val in lines:
            fh.write(f"{key}\t{val}\n")

    logger.debug("Wrote manifest: %s", manifest_path)


# -----------------------------------------------------------
# Webin-CLI execution
# -----------------------------------------------------------


def _build_webin_cli_cmd(
    manifest_path: Path,
    username: str,
    cmd_prefix: list[str],
    test: bool,
) -> list[str]:
    """Build the webin-cli command list for a reads submission.

    Args:
        manifest_path: Path to the manifest file.
        username: ENA Webin username.
        cmd_prefix: Base command (either ``ena-webin-cli``
            or ``java -jar webin-cli.jar``).
        test: Whether to target the test server.

    Returns:
        Complete command list.
    """
    cmd = cmd_prefix + [
        "-context=reads",
        f"-manifest={manifest_path}",
        f"-userName={username}",
        f"-passwordEnv={_PASSWORD_ENV}",
        "-submit",
    ]
    if test:
        cmd.append("-test")
    return cmd


def _handle_webin_failure(stdout: str) -> tuple[str, int]:
    """Interpret a non-zero webin-cli exit as recoverable or not.

    Mirrors the upstream ``handle_webin_failures`` logic.

    Args:
        stdout: Captured stdout from webin-cli.

    Returns:
        Tuple of (*message*, *exit_code*) where exit_code 0
        means treat as success (object already exists).
    """
    if "Invalid submission account user name or password." in stdout:
        return "Invalid credentials for Webin account.", 1
    if (
        "The object being added already exists"
        " in the submission account with accession:" in stdout
    ):
        return "Submission already exists — treated as success.", 0
    return stdout, 1


def run_webin_cli(
    manifest_path: Path,
    username: str,
    cmd_prefix: list[str],
    test: bool,
    retries: int = _DEFAULT_RETRIES,
    retry_delay: int = _DEFAULT_RETRY_DELAY,
) -> subprocess.CompletedProcess[str]:
    """Execute webin-cli with retry / exponential backoff.

    Args:
        manifest_path: Path to the manifest file.
        username: ENA Webin username.
        cmd_prefix: Base command prefix (jar or CLI path).
        test: Whether to target the test server.
        retries: Maximum number of attempts.
        retry_delay: Initial delay between retries (seconds).

    Returns:
        Completed process on success.

    Raises:
        typer.Exit: On invalid credentials or after all
            retries are exhausted.
    """
    cmd = _build_webin_cli_cmd(
        manifest_path, username, cmd_prefix, test,
    )

    for attempt in range(1, retries + 1):
        logger.info(
            "Running webin-cli (attempt %d/%d)...",
            attempt, retries,
        )
        logger.debug("Command: %s", " ".join(cmd))

        result = subprocess.run(
            cmd,
            shell=False,
            capture_output=True,
            text=True,
            env=os.environ.copy(),
        )

        if result.returncode == 0:
            logger.info("webin-cli completed successfully")
            return result

        logger.error(
            "webin-cli failed (exit %d)", result.returncode,
        )
        logger.debug("stdout: %s", result.stdout)
        logger.debug("stderr: %s", result.stderr)

        message, exit_code = _handle_webin_failure(result.stdout)

        if "Invalid credentials" in message:
            logger.error("%s", message)
            raise typer.Exit(1)

        if exit_code == 0:
            logger.info("%s", message)
            return result

        if attempt < retries:
            sleep_time = retry_delay * (2 ** (attempt - 1))
            logger.warning(
                "Retrying in %d seconds...", sleep_time,
            )
            time.sleep(sleep_time)
        else:
            logger.error(
                "All %d attempts failed for %s",
                retries, manifest_path,
            )
            raise typer.Exit(1)

    # unreachable, but satisfies type checker
    raise typer.Exit(1)


# -----------------------------------------------------------
# Result parsing
# -----------------------------------------------------------

_RUN_ACCESSION_RE: Final = re.compile(
    r"\b([ESD]RR\d{6,})\b"
)


def parse_webin_result(
    run: dict[str, Any],
    proc: subprocess.CompletedProcess[str],
) -> dict[str, Any]:
    """Extract accession and status from webin-cli output.

    Args:
        run: Original run metadata dict.
        proc: Completed webin-cli subprocess.

    Returns:
        Result dict with name, accession, and status.
    """
    combined = proc.stdout + "\n" + proc.stderr
    accessions = _RUN_ACCESSION_RE.findall(combined)

    already_exists = (
        "The object being added already exists" in combined
    )
    status = "DUPLICATE" if already_exists else "SUBMITTED"

    return {
        "name": _run_name(run),
        "accession": accessions[0] if accessions else "",
        "all_accessions": accessions,
        "status": status,
    }


# -----------------------------------------------------------
# File loading (CSV, TSV, XLS, XLSX, JSON)
# -----------------------------------------------------------


def _is_metadata_row(row: Sequence[object]) -> bool:
    """Check whether *row* is a DataHarmonizer label row."""
    non_empty = sum(
        1 for c in row
        if c is not None and str(c).strip()
    )
    return non_empty <= 1


def extract_runs_from_tabular(
    filepath: str | Path,
    delimiter: str = ",",
) -> list[dict[str, str]]:
    """Extract run dicts from a CSV or TSV file.

    Args:
        filepath: Path to the tabular file.
        delimiter: Column delimiter character.

    Returns:
        List of run dicts.
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

    runs: list[dict[str, str]] = []
    for row in rows[idx:]:
        run: dict[str, str] = {}
        for col, val in zip(headers, row):
            col = col.strip()
            if col and val is not None and val.strip():
                run[col] = val.strip()
        if run:
            runs.append(run)

    return runs


def extract_runs_from_excel(
    filepath: str | Path,
) -> list[dict[str, str]]:
    """Extract run dicts from an XLS or XLSX file.

    Args:
        filepath: Path to the spreadsheet file.

    Returns:
        List of run dicts.
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
            [list(r) for r in ws.iter_rows(values_only=True)]
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

    runs: list[dict[str, str]] = []
    for row in rows[idx:]:
        run: dict[str, str] = {}
        for col, val in zip(headers, row):
            if not col:
                continue
            if val is not None and str(val).strip():
                run[col] = str(val).strip()
        if run:
            runs.append(run)

    return runs


def extract_runs_from_json(
    input_data: object,
) -> list[dict[str, Any]] | None:
    """Extract run dicts from a DataHarmonizer JSON export.

    Args:
        input_data: Parsed JSON data (any shape).

    Returns:
        List of run dicts, or ``None`` if unrecognised.
    """
    if isinstance(input_data, list):
        return input_data

    if isinstance(input_data, dict):
        container = input_data.get("Container")
        if isinstance(container, dict):
            for key, val in container.items():
                if isinstance(val, list):
                    logger.info(
                        "Extracted runs from Container.%s",
                        key,
                    )
                    return val
        if "runs" in input_data:
            return input_data["runs"]
        if "data" in input_data:
            return input_data["data"]
        return [input_data]

    return None


def load_input_file(
    filepath: str | Path,
) -> list[dict[str, Any]] | None:
    """Load run data from a supported file format.

    Supported formats: JSON, CSV, TSV, XLS, XLSX.

    Args:
        filepath: Path to the input file.

    Returns:
        List of run dicts, or ``None`` if unrecognised.
    """
    ext = Path(filepath).suffix.lower()
    if ext == ".json":
        with open(filepath) as fh:
            return extract_runs_from_json(json.load(fh))
    if ext == ".csv":
        return extract_runs_from_tabular(filepath, delimiter=",")
    if ext == ".tsv":
        return extract_runs_from_tabular(
            filepath, delimiter="\t",
        )
    if ext in (".xlsx", ".xls"):
        return extract_runs_from_excel(filepath)
    return None


# -----------------------------------------------------------
# Result output
# -----------------------------------------------------------


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


# -----------------------------------------------------------
# Main
# -----------------------------------------------------------


@app.command()
def main(
    input_file: Path = typer.Option(
        ..., "--input", exists=True,
        help="Run metadata file (JSON, CSV, TSV, XLS, XLSX)."
        " One row = one run.",
    ),
    linkml: Path = typer.Option(
        ..., exists=True,
        help="LinkML YAML schema for pre-flight validation"
        " (e.g. schemas/SRA_experiment.yaml)",
    ),
    xsd: Path = typer.Option(
        ..., exists=True,
        file_okay=False, resolve_path=True,
        help="Directory containing SRA.experiment.xsd"
        " and SRA.common.xsd",
    ),
    test: bool = typer.Option(
        False, "--test",
        help="Target the ENA test server",
    ),
    log: Path | None = typer.Option(
        None, help="Path to log file",
    ),
    output: Path | None = typer.Option(
        None,
        help="Path to write JSON accession results"
        " (default: stdout)",
    ),
    workdir: Path | None = typer.Option(
        None, "--workdir",
        help="Directory for temporary manifest files"
        " (default: system temp)",
    ),
    webin_cli_jar: Path | None = typer.Option(
        None, "--webin-cli-jar",
        help="Path to a pre-downloaded webin-cli jar file",
    ),
    download_webin_cli: bool = typer.Option(
        False, "--download-webin-cli",
        help="Download the latest webin-cli jar automatically",
    ),
    download_webin_cli_version: str | None = typer.Option(
        None, "--download-webin-cli-version",
        help="Specific webin-cli version to download"
        " (default: latest)",
    ),
    download_webin_cli_dir: Path = typer.Option(
        Path("."), "--download-webin-cli-dir",
        help="Directory in which to save the downloaded jar"
        " (default: current directory)",
    ),
    max_results: int = typer.Option(
        5000, "--max-results",
        help="Max runs to fetch from Reports API for"
        " duplicate checking",
    ),
    retries: int = typer.Option(
        _DEFAULT_RETRIES, "--retries",
        help=f"webin-cli retry attempts"
        f" (default: {_DEFAULT_RETRIES})",
    ),
    retry_delay: int = typer.Option(
        _DEFAULT_RETRY_DELAY, "--retry-delay",
        help=f"Initial retry delay in seconds"
        f" (default: {_DEFAULT_RETRY_DELAY})",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="Validate and generate manifests but do not"
        " call webin-cli",
    ),
) -> None:
    """Submit reads to ENA via webin-cli (reads context)."""
    setup_logging(log)

    # -- Credentials -------------------------------------
    username = os.environ.get("ENA_USERNAME", "").strip()
    password = os.environ.get("ENA_PASSWORD", "").strip()
    if not username or not password:
        logger.error(
            "ENA_USERNAME and ENA_PASSWORD environment"
            " variables must be set",
        )
        raise typer.Exit(1)

    env_label = "TEST" if test else "PRODUCTION"
    logger.info(
        "ENA Reads Submission — environment: %s", env_label,
    )
    logger.debug("Auth username: %s", username)

    auth = HTTPBasicAuth(username, password)

    # -- Optional: download webin-cli jar ----------------
    jar_path: Path | None = webin_cli_jar
    if download_webin_cli:
        download_webin_cli_dir.mkdir(parents=True, exist_ok=True)
        jar_path = _download_webin_cli(
            dest_dir=download_webin_cli_dir,
            version=download_webin_cli_version,
        )

    # Locate webin-cli (not needed for dry-run, but check early)
    if not dry_run:
        _, cmd_prefix = find_webin_cli(jar_path)
    else:
        cmd_prefix = []  # unused in dry-run

    # -- Step 1: Load input file -------------------------
    logger.info("Loading input: %s", input_file)
    runs = load_input_file(input_file)
    if runs is None:
        logger.error(
            "Unsupported file format."
            " Supported: .json, .csv, .tsv, .xlsx, .xls",
        )
        raise typer.Exit(1)

    logger.info("Loaded %d run(s) from input", len(runs))

    # -- Step 2: Duplicate detection ---------------------
    logger.info(
        "Fetching account runs from Webin Reports API...",
    )
    account_runs = fetch_account_runs(
        auth, use_test=test, max_results=max_results,
    )
    for r in account_runs:
        logger.info(
            "  Account run: %s | name=%s | status=%s",
            r["accession"], r["name"], r["status"],
        )

    logger.info("Checking for duplicate runs...")
    duplicates = find_duplicate_runs(runs, account_runs)

    results: dict[str, list[dict[str, Any]]] = {
        "duplicates": [],
        "submitted": [],
        "failed": [],
    }

    if duplicates:
        logger.warning(
            "Found %d duplicate(s) — will NOT be submitted:",
            len(duplicates),
        )
        for idx, dup_info in duplicates.items():
            name = _run_name(runs[idx])
            logger.warning(
                "  DUPLICATE: '%s' matches %s (accession: %s)",
                name, dup_info["match_reason"],
                dup_info["accession"],
            )
            results["duplicates"].append({
                "input_index": idx,
                "name": name,
                "existing_accession": dup_info["accession"],
                "match_reason": dup_info["match_reason"],
            })

    runs_to_submit = [
        r for i, r in enumerate(runs) if i not in duplicates
    ]

    if not runs_to_submit:
        logger.info(
            "No new runs to submit"
            " (all are duplicates or input is empty)",
        )
        _write_results(results, output)
        return

    logger.info(
        "%d run(s) to submit after duplicate check",
        len(runs_to_submit),
    )

    # -- Step 3: LinkML validation -----------------------
    logger.info("Loading LinkML schema: %s", linkml)
    schema = load_linkml_schema(linkml)

    logger.info("Validating input against LinkML schema...")
    linkml_valid, linkml_messages = validate_against_linkml(
        runs_to_submit, schema,
    )
    for msg in linkml_messages:
        logger.info("  %s", msg)

    if not linkml_valid:
        logger.error(
            "LinkML validation FAILED — aborting submission",
        )
        raise typer.Exit(1)

    logger.info("LinkML validation PASSED")

    # -- Step 4: XSD validation -------------------------
    logger.info(
        "Validating experiment XML against XSD: %s", xsd,
    )
    xsd_valid, xsd_messages = validate_against_xsd(
        runs_to_submit, xsd,
    )
    for msg in xsd_messages:
        logger.info("  %s", msg)

    if not xsd_valid:
        logger.error(
            "XSD validation FAILED — aborting submission",
        )
        raise typer.Exit(1)

    logger.info("XSD validation PASSED")

    # -- Step 5: Manifest field validation ---------------
    logger.info(
        "Validating webin-cli manifest fields and files...",
    )
    fields_valid, field_messages = validate_run_fields(
        runs_to_submit,
    )
    for msg in field_messages:
        logger.info("  %s", msg)

    if not fields_valid:
        logger.error(
            "Manifest field validation FAILED"
            " — aborting submission",
        )
        raise typer.Exit(1)

    logger.info("Manifest field validation PASSED")

    # -- Step 6: Generate manifests ----------------------
    use_tmpdir = workdir is None
    tmp_root: tempfile.TemporaryDirectory[str] | None = None
    manifest_dir: Path

    if use_tmpdir:
        tmp_root = tempfile.TemporaryDirectory(
            prefix="ena_submit_reads_",
        )
        manifest_dir = Path(tmp_root.name)
    else:
        manifest_dir = workdir  # type: ignore[assignment]
        manifest_dir.mkdir(parents=True, exist_ok=True)

    try:
        manifest_paths: list[Path] = []
        for i, run in enumerate(runs_to_submit):
            name = _run_name(run) or f"run_{i}"
            safe_name = re.sub(r"[^\w\-.]", "_", name)
            mpath = manifest_dir / f"{safe_name}.manifest"
            build_manifest(run, mpath)
            manifest_paths.append(mpath)
            logger.info(
                "Generated manifest: %s", mpath,
            )

        if dry_run:
            logger.info("DRY RUN — skipping webin-cli calls")
            for mpath in manifest_paths:
                logger.info(
                    "Manifest content:\n%s",
                    mpath.read_text(),
                )
            _write_results(results, output)
            return

        # -- Step 7: Submit via webin-cli ----------------
        for run, mpath in zip(runs_to_submit, manifest_paths):
            name = _run_name(run)
            logger.info(
                "Submitting run '%s' via webin-cli...", name,
            )
            try:
                proc = run_webin_cli(
                    manifest_path=mpath,
                    username=username,
                    cmd_prefix=cmd_prefix,
                    test=test,
                    retries=retries,
                    retry_delay=retry_delay,
                )
                run_result = parse_webin_result(run, proc)
                logger.info(
                    "  SUBMITTED: name=%s accession=%s"
                    " status=%s",
                    run_result["name"],
                    run_result["accession"],
                    run_result["status"],
                )
                results["submitted"].append(run_result)
            except typer.Exit:
                logger.error(
                    "  FAILED: run '%s'", name,
                )
                results["failed"].append({
                    "name": name,
                    "error": "webin-cli execution failed",
                })

    finally:
        if tmp_root is not None:
            tmp_root.cleanup()

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
            "    %s -> %s", d["name"], d["existing_accession"],
        )
    logger.info(
        "  Newly submitted: %d", len(results["submitted"]),
    )
    for s in results["submitted"]:
        logger.info(
            "    %s -> %s", s["name"], s["accession"],
        )
    if results["failed"]:
        logger.error(
            "  Failed: %d", len(results["failed"]),
        )
        for f in results["failed"]:
            logger.error("    %s", f["name"])
    logger.info("=" * 60)


if __name__ == "__main__":
    app()
