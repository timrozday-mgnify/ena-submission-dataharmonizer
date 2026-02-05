# ena-submission-dataharmonizer
Generate DataHarmonizer schema to support data entry and generation of manifests for a ENA sample/run submission.

## `scripts/ena_to_linkml.py`
The script has three main stages: parse XML, convert to LinkML, and write YAML. Here's how each works:

  1. XML Parsing (parse_checklist_xml / _parse_field)

  Reads an ENA checklist XML file using xml.etree.ElementTree. It navigates the tree structure:

  - Checklist metadata: Extracts the accession ID (e.g. ERC000015), label, name, and description from CHECKLIST_SET > CHECKLIST > DESCRIPTOR.
  - Field groups: Iterates over each FIELD_GROUP element, capturing the group name (e.g. "Collection event information").
  - Fields: For each FIELD within a group, _parse_field extracts:
    - NAME, LABEL, DESCRIPTION, MANDATORY, MULTIPLICITY as plain text
    - Field type — determines which of three variants is present:
        - TEXT_FIELD (empty) — plain free-text
      - TEXT_FIELD with a REGEX_VALUE child — free-text with a regex constraint
      - TEXT_CHOICE_FIELD — an enumeration; collects all TEXT_VALUE/VALUE strings
    - Units — collects any UNIT elements (e.g. °C, m, kg)

  The result is a nested dict of checklist metadata → field groups → fields.

  2. LinkML Conversion (convert_to_linkml)

  Transforms the parsed dict into a LinkML schema dict. Iterates all field groups and fields in document order, building three things simultaneously:

  - Slots (_build_slot): Each field becomes a LinkML slot with:
    - name, title (from LABEL), description
    - range: set to an enum name (PascalCase + "Menu") for choice fields, or "string" otherwise
    - required: true if MANDATORY was "mandatory"
    - pattern: the raw regex string, if present
    - comments: unit information as "Allowed units: °C, m" if units exist
  - Enums (_build_enum): Each TEXT_CHOICE_FIELD becomes a LinkML enum. The enum name is derived by converting the snake_case field name to PascalCase and appending "Menu" (e.g. trophic_level → TrophicLevelMenu). Each choice value becomes a permissible_values entry with a text
  property.
  - Slot usage: Each field gets a sequential rank (for column ordering in DataHarmonizer) and a slot_group (the field group name, used for section headers).

  These are assembled into a schema dict containing:
  - Top-level metadata (id, name, title, description, prefixes, imports)
  - A dh_interface base class (required by DataHarmonizer)
  - A main class (named by accession, e.g. ERC000015) that inherits from dh_interface and references all slots with their usage overrides

  3. YAML Output (write_yaml)

  Writes the schema dict to YAML using a custom dumper (_LinkMLDumper) that:
  - Emits lowercase true/false (LinkML convention, vs PyYAML's default True/False)
  - Uses literal block style (|) for multi-line strings
  - Preserves dict insertion order (sort_keys=False)
  - Handles Unicode characters in unit strings (allow_unicode=True)

  4. CLI (main)

  Uses argparse to accept either explicit file paths or scan a directory for *.xml files. For each input file it runs the parse → convert → write pipeline, then prints a summary of field/enum counts.

## `scripts/merge_linkml.py`

Merges two or more DataHarmonizer LinkML YAML schema files into a single combined schema. Input files are listed as positional arguments in **priority order** (first = highest priority). When identically named slots, enums, or slot_usage entries appear in multiple inputs, the definition from the highest-priority file is kept.

Slots from all inputs are included in the output. Ordering is determined by walking inputs from highest to lowest priority and appending each slot the first time it is seen, so the highest-priority file's column order is preserved and fields unique to lower-priority files follow. Ranks are renumbered sequentially.

**Usage:**

```bash
python scripts/merge_linkml.py <input1.yaml> <input2.yaml> [input3.yaml ...] -o <output.yaml>

# Example:
python scripts/merge_linkml.py schemas/ERC000015.yaml schemas/ERC000025.yaml \
    -o schemas/merged.yaml \
    --name MergedChecklist \
    --title "Merged ENA checklist"
```

**Options:**

| Flag | Description |
|---|---|
| `-o, --output` | Output file path (required) |
| `--name` | Schema name (default: from highest-priority input) |
| `--title` | Schema title (default: from highest-priority input) |
| `--description` | Schema description (default: from highest-priority input) |
| `--base-uri` | Base URI for the schema id (default: derived from highest-priority input) |

### Implementation details

**1. YAML helpers (lines 31–48)**

A custom _LinkMLDumper class extends yaml.SafeDumper with two custom representers, identical to the ones in ena_to_linkml.py:
- Booleans are emitted as lowercase true/false (LinkML convention, vs PyYAML's default True/False)
- Strings containing newlines use YAML literal block style (|)

**2. Loading and extraction (lines 55–78)**

- load_schema() reads a YAML file via yaml.safe_load and returns the parsed dict.
- _get_main_class() finds the DataHarmonizer main class by scanning the classes section for the entry with is_a: dh_interface. Returns its name and dict.
- _ordered_slot_names() returns the slot name list from that main class, preserving the original ordering.

**3. Merging (lines 85–220)**

This is split into two functions:

merge_schemas(schemas) (line 85) takes a list of parsed schema dicts, highest priority first. It iterates through each schema in priority order and collects three things using a first-seen-wins strategy:

- Slots: For each schema, walks the main class's slot list via _ordered_slot_names(). The first time a slot name is encountered, its definition from the slots section and its slot_usage entry (rank, slot_group) are recorded. Later schemas with the same slot name are ignored. A second pass (line 122) catches any
orphan slot definitions that exist in a slots section but weren't referenced in any main class's slot list.
- Enums: Same first-seen-wins approach — iterates enums from each schema and keeps only the first definition of each enum name.
- Ordering: Slot names are appended to seen_slot_order the first time they appear, so the highest-priority file's column order is preserved and fields unique to lower-priority files are appended after.

Finally, ranks are renumbered sequentially (line 129) so the merged output has contiguous ranks 1, 2, 3, ... regardless of gaps caused by merging.

build_merged_schema() (line 143) assembles the final LinkML schema dict from the merge results plus metadata. It:
- Defaults name, title, description, and base_uri from the highest-priority input if the caller didn't supply them
- Merges prefixes from all inputs (iterating in reverse so higher-priority values overwrite lower)
- Constructs the standard DataHarmonizer class structure: a dh_interface base class and a main class that inherits from it, with the merged slot list and renumbered slot_usage
- Includes the merged enums section if any enums exist

**4. CLI (lines 246–325)**

main() uses argparse to accept:
- One or more positional input files (priority order: first = highest)
- -o for the required output path
- Optional --name, --title, --description, --base-uri to override merged schema metadata

It validates that all input files exist, loads them, calls merge_schemas() then build_merged_schema(), writes the result via write_yaml(), and prints a summary of slot/enum counts.

## `scripts/filter_linkml.py`

Here's how filter_linkml.py works, section by section:

**YAML helpers (lines 33–50)**

Reuses the same custom dumper pattern from ena_to_linkml.py and merge_linkml.py. _LinkMLDumper extends yaml.SafeDumper with two custom representers:

- _bool_representer — emits true/false (lowercase) instead of Python's default True/False, matching LinkML conventions.
- _str_representer — uses YAML literal block style (|) for multi-line strings, plain style otherwise.

**Loading (lines 57–74)**

- load_schema — standard yaml.safe_load from file.
- load_field_list — reads a text file line-by-line, stripping whitespace, skipping blanks and #-prefixed comment lines. Returns a plain list of field name strings.

**Schema helpers (lines 81–96)**

- _get_main_class — iterates the classes dict to find the one with is_a: dh_interface. This is the DataHarmonizer convention: each schema has a dh_interface base class and one concrete class that extends it. Returns (name, class_dict).
- _referenced_enums — collects the set of all range values from a slots dict. Used later to determine which enums are still needed after filtering.

Core filtering logic (lines 103–196)

filter_schema(schema, include, exclude) does the work:

1. Resolve the main class and get its full slot list (all_slot_names). Early-returns the schema unchanged if no main class is found.
2. Warn on unknown fields (lines 128–138) — any names in the include or exclude lists that aren't in the schema's slot list get reported to stderr.
3. Determine which slots to keep (lines 140–150):
  - If include is provided, filter all_slot_names to only those in the include set. The original schema order is preserved (not the order of the include file).
  - If exclude is provided, remove those from whatever list remains.
  - This means with both flags, include narrows first, then exclude removes from that subset.
4. Build the filtered schema (lines 152–196):
  - Copies all top-level keys except classes, slots, and enums as-is (lines 153–157).
  - Rebuilds slot_usage (lines 160–165): iterates kept slots with enumerate(..., start=1) to assign contiguous ranks. Copies the original slot_usage entry for each kept slot, overwriting only rank.
  - Rebuilds the main class (lines 167–181): iterates the original main class dict key-by-key, substituting slots and slot_usage with the filtered versions, preserving all other keys (name, title, description, is_a). Non-main classes (like dh_interface) pass through untouched.
  - Filters top-level slots (lines 184–186): dict comprehension keeping only entries whose name is in kept.
  - Prunes enums (lines 189–194): calls _referenced_enums on the surviving slots to get the set of enum names still used as a range. Only those enums are kept. If none remain, the enums key is omitted entirely.

**CLI (lines 222–291)**

main() wires it together with argparse:

- Positional input_file, required -o/--output, optional --include and --exclude (at least one required, enforced by parser.error at line 251).
- Validates the input file exists, loads everything, calls filter_schema, writes output, then prints a summary line showing original count, filtered count, removed count, required count, and enum count.

## `scripts/build_linkml.py`

The script is an orchestrator that chains the three existing sibling scripts into a single pipeline. Here's how it works:

**Import strategy (lines 25–30)**

Line 26 prepends the script's own directory to sys.path so it can import from the three sibling modules by name. It then pulls in exactly the functions it needs:

- ena_to_linkml — XML parsing (parse_checklist_xml) and LinkML conversion (convert_to_linkml)
- merge_linkml — YAML loading (load_schema), merging (merge_schemas, build_merged_schema), and output (write_yaml)
- filter_linkml — field list loading (load_field_list) and slot filtering (filter_schema)

No YAML dumper or helper code is defined locally — write_yaml from merge_linkml brings its own.

**CLI (lines 33–91)**

Argparse defines two input groups (--xsd, --linkml), each accepting one or more files via nargs="+". The output (-o) is required. Optional arguments include --include/--exclude filter files, and metadata overrides (--name, --title, --description, --base-uri).

**Validation (lines 93–107)**

Two checks happen before any processing:

1. At least one input source (line 94) — parser.error() if neither --xsd nor --linkml is provided.
2. All files exist (lines 98–107) — builds a flat list of every file path (inputs + filter lists), checks each with os.path.isfile, and exits early with an error message if any are missing. This fails fast rather than erroring midway through processing.

Pipeline steps

**Step 1 — XSD conversion (lines 109–117):** For each --xsd file, calls parse_checklist_xml to get a structured dict from the ENA XML, then convert_to_linkml to produce an in-memory LinkML schema dict. These stay in a list, never written to disk.

**Step 2 — LinkML loading (lines 119–125):** For each --linkml file, calls load_schema (which is just yaml.safe_load) to parse the YAML into a dict.

**Step 3 — Merge (lines 127–139)**: Concatenates the two lists as linkml_schemas + xsd_schemas. This ordering is significant — merge_schemas treats earlier entries as higher priority, so LinkML files always override XSD files for identically named slots/enums. merge_schemas returns a flat result with deduplicated
slots, enums, and renumbered ranks. build_merged_schema then wraps that into a complete LinkML schema dict, using the caller's metadata overrides or falling back to the highest-priority input's values.

**Step 4 — Filter (lines 141–151):** Only runs if --include or --exclude was provided. Loads the field name lists via load_field_list (which strips whitespace, skips blank/comment lines), then calls filter_schema. That function removes slots from the class, slot_usage, and top-level slots dict, prunes enums no longer
referenced by any remaining slot's range, and renumbers ranks contiguously.

**Step 5 — Write (lines 153–155):** write_yaml dumps the final schema dict to YAML using the custom _LinkMLDumper (lowercase booleans, literal blocks for multiline strings), creating parent directories if needed.

**Summary (lines 157–162):** Prints slot count, required count, and enum count so the user can sanity-check the output without opening the file.

Design notes

- A single XSD input with no filtering works fine — the merge step passes through a single schema unchanged.
- The filtering step is skipped entirely when no filter flags are given, so there's no overhead or behavioral change.
- The --base-uri default is hardcoded to the project's GitHub URL, matching ena_to_linkml.py's default. For XSD conversion this is used directly; for merge it's passed to build_merged_schema which uses it to construct the output schema's id field.


## `scripts/xsd_to_linkml.py`

The script converts ENA/SRA XSD schema files to DataHarmonizer-compatible LinkML YAML. It uses a recursive depth-first walker to extract flat fields from deeply nested XSD complex types.

Architecture

┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│  parse_xsd()    │────▶│   XSDWalker     │────▶│ convert_xsd_to_ │
│  Extract named  │     │   Recursive     │     │ linkml()        │
│  types into     │     │   tree walker   │     │ Build schema    │
│  lookup dicts   │     │                 │     │ dict            │
└─────────────────┘     └─────────────────┘     └─────────────────┘

---
1. YAML Helpers (lines 25-42)

_LinkMLDumper ensures LinkML-compatible output:
- Lowercase booleans: true/false instead of Python's True/False
- Block style for multiline strings: Uses | style for descriptions with newlines

---
2. Constants (lines 45-80)

Skip patterns (line 53-64):
SKIP_ELEMENT_PATTERNS = r"^(.*_LINKS|.*_ATTRIBUTES|RELATED_.*)$"
SKIP_COM_TYPES = {"com:LinkType", "com:AttributeType", ...}
These filter out ENA metadata containers that aren't useful as DataHarmonizer fields.

Type mapping (lines 67-80):
XSD_TO_LINKML_TYPE = {
    "xs:string": "string",
    "xs:int": "integer",
    "xs:dateTime": "datetime",
    ...
}

---
3. Parsing Functions (lines 83-160)

_get_doc(elem) - Extracts documentation from xs:annotation/xs:documentation

_extract_inline_enum(simple_type_elem) - Extracts enumeration values from a xs:restriction with xs:enumeration children

parse_xsd(filepath) - Parses the XSD and builds lookup dictionaries:
complex_types = {"StudyType": <Element>, "OrganismType": <Element>, ...}
simple_types = {"typeLibraryStrategy": <Element>, ...}

_find_main_type() - Maps filename to entry point:
- SRA.study.xsd → StudyType
- ENA.project.xsd → ProjectType
- etc.

---
 XSDWalker Class (lines 193-477)

The core recursive walker with these key methods:

State Management

self.slots = {}       # Accumulated LinkML slots
self.enums = {}       # Accumulated LinkML enums
self.seen_names = set()  # Deduplication (first-seen-wins)

walk_complex_type(ct_elem, force_optional) (lines 343-369)

Entry point for walking a complexType. Handles:
1. Extension bases: If <xs:complexContent><xs:extension base="...">, recurse into base type first
2. Direct attributes: Process any xs:attribute children
3. Child containers: Find and walk all xs:sequence, xs:all, xs:choice

_process_element(elem, force_optional) (lines 385-447)

Processes a single xs:element. Decision tree:

┌─ type="typeLibraryStrategy" (named simpleType with enum)
│  └─▶ Create slot + enum (e.g., LIBRARY_STRATEGY → LibraryStrategyMenu)
│
├─ type="xs:string" (primitive)
│  └─▶ Create slot with range "string"
│
├─ type="OrganismType" (named complexType)
│  └─▶ Recurse into that complexType
│
├─ type="com:RefObjectType" (external reference)
│  └─▶ Create string slot (reference field)
│
├─ Inline <xs:complexType> child
│  └─▶ _process_inline_complex_type()
│
├─ Inline <xs:simpleType> child with enums
│  └─▶ Create slot + enum
│
└─ No type specified
   └─▶ Create string slot

_process_inline_complex_type() (lines 449-477)

Handles inline complex types. Key pattern: choice-as-enum

For LIBRARY_LAYOUT which contains:
<xs:choice>
  <xs:element name="SINGLE">...</xs:element>
  <xs:element name="PAIRED">...</xs:element>
</xs:choice>

Creates:
- Slot LIBRARY_LAYOUT with range LibraryLayoutMenu
- Enum with values SINGLE, PAIRED
- Also extracts attributes from choice children (like NOMINAL_LENGTH from PAIRED)

rocess_choice_as_enum() (lines 300-341)

Converts xs:choice children into enum values. If choice options have attributes (like PAIRED has NOMINAL_LENGTH), those are also extracted as optional slots.

---
5. Schema Assembly (lines 484-566)

convert_xsd_to_linkml() orchestrates the conversion:

1. Parse XSD → get type lookups
2. Find main type (e.g., StudyType)
3. Create walker and walk the type tree
4. Build schema dict matching existing format:

schema = {
    "id": "https://github.com/.../SRA_study",
    "name": "SRA_study",
    "classes": {
        "dh_interface": {...},
        "SRA_study": {
            "slots": [...],
            "slot_usage": {"STUDY_TITLE": {"rank": 1}, ...}
        }
    },
    "slots": {...},
    "enums": {...}
}

---
6. CLI (lines 593-656)

Standard argparse CLI matching ena_to_linkml.py:
- Default: process all *.xsd except SRA.common.xsd
- Supports -i input dir, -o output dir, --base-uri

---
Key Design Decisions

1. First-seen-wins deduplication: When the same field appears multiple times (e.g., ORGANISM in both SUBMISSION_PROJECT and UMBRELLA_PROJECT), only the first is kept
2. Optionality propagation: Children of xs:choice or elements with minOccurs="0" are marked optional
3. Flat output: Nested structures are flattened - only leaf fields become slots
4. Choice-as-enum: xs:choice with element children becomes an enum, not multiple exclusive fields
