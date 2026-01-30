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