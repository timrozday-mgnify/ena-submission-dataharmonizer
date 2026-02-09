# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Pipeline for generating DataHarmonizer LinkML schemas from ENA/SRA source formats (XSD schemas, ENA XML checklists, and existing LinkML YAML files). The output schemas drive DataHarmonizer's spreadsheet-like data entry UI for ENA sample/run submissions.

## Commands

```bash
# Build a merged schema from mixed inputs (auto-detects .yaml/.xsd/.xml)
python scripts/build_linkml.py input1.yaml input2.xsd input3.xml -o output.yaml

# Build with filtering
python scripts/build_linkml.py schemas/*.yaml assets/ena_schema/*.xsd \
    --include include.txt --exclude exclude.txt -o output.yaml

# Individual conversion scripts
python scripts/ena_to_linkml.py assets/ena_schema/ERC000015.xml -o schemas/
python scripts/xsd_to_linkml.py assets/ena_schema/SRA.study.xsd -o schemas/
python scripts/merge_linkml.py schema1.yaml schema2.yaml -o merged.yaml
python scripts/filter_linkml.py input.yaml --include list.txt -o filtered.yaml

# Interactive terminal schema editor (Textual UI)
python scripts/edit_linkml.py

# Tests
pytest scripts/test_edit_linkml.py -v
```

## Dependencies

Managed via mamba/conda with pip sub-dependencies. See `environment.yml` for the full spec and `requirements.txt` for a pip-only alternative.

**Setup (mamba):**
```bash
mamba env create -f environment.yml
mamba activate dataharmonizer-dev
```

**Key packages (conda-forge):** python 3.11, PyYAML, linkml-runtime
**Key packages (pip):** textual, rich, elasticsearch (v8.x), pytest, pytest-asyncio

**Docker services:** Elasticsearch 8.17.0 (see `docker-compose.yaml`, required for search in `edit_linkml.py` and ES integration tests)

## Architecture

**Pipeline flow:** Source files → convert to LinkML dicts → merge (priority order) → filter (optional) → write YAML

`build_linkml.py` is the main orchestrator that inlines functionality from the other scripts. The individual scripts (`ena_to_linkml.py`, `xsd_to_linkml.py`, `merge_linkml.py`, `filter_linkml.py`) also work standalone. `edit_linkml.py` is a separate Textual-based interactive editor.

**Input priority:** File order on the command line determines merge priority (first = highest). When slots/enums share a name, the first-seen-wins.

**Source directories:**
- `assets/ena_schema/` — Source XSD and XML files from ENA/SRA
- `schemas/` — Generated and curated LinkML YAML output files

## LinkML Schema Conventions

All schemas follow the DataHarmonizer convention:
- Must have a `dh_interface` base class and one main class with `is_a: dh_interface`
- The main class has `slots` (field name list), `slot_usage` (rank for column order, slot_group for section headers)
- Top-level `slots` section contains field definitions (name, title, description, range, required, pattern, comments)
- Top-level `enums` section contains enum definitions with `permissible_values`
- Enum naming convention: PascalCase field name + "Menu" suffix (e.g., `trophic_level` → `TrophicLevelMenu`)

## YAML Output Requirements

All scripts use a custom `_LinkMLDumper` (duplicated in each script) that enforces:
- Lowercase booleans (`true`/`false`, not Python's `True`/`False`)
- Literal block style (`|`) for multiline strings
- Preserved dict insertion order (`sort_keys=False`)

## Key Patterns

- **`_get_main_class()`** — Finds the class with `is_a: dh_interface`; used across merge, filter, and edit scripts
- **Rank renumbering** — After any merge/filter/edit operation, ranks are reassigned sequentially (1, 2, 3...)
- **Enum pruning** — After filtering slots, unreferenced enums are removed automatically
- **XSD flattening** — `xsd_to_linkml.py` uses `XSDWalker` to recursively walk nested complex types into flat slots; `xs:choice` elements become enums
