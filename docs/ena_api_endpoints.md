# ENA API Endpoints

Summary of the ENA/Webin API endpoints used by the submission scripts in this project.

---

## Webin v2 Submission API

Used by `submit_study.py` and `submit_sample.py` via `ena_common.submit_xml()`.

| Environment | Base URL |
|-------------|----------|
| Production  | `https://www.ebi.ac.uk/ena/submit/webin-v2` |
| Test        | `https://wwwdev.ebi.ac.uk/ena/submit/webin-v2` |

### `POST /submit`

Submit an XML document containing study or sample metadata.

- **Full URL:** `{base_url}/submit`
- **Auth:** HTTP Basic (`ENA_USERNAME` / `ENA_PASSWORD`)
- **Content-Type:** `application/xml`
- **Accept:** `application/xml`
- **Body:** A `<WEBIN>` document containing a `<SUBMISSION_SET>` (with `<ADD>` or `<MODIFY>` action) and either a `<PROJECT_SET>` (studies) or `<SAMPLE_SET>` (samples).
- **Response:** ENA receipt XML. The root element has a `success` attribute (`"true"` / `"false"`). On success, child elements carry accession numbers and status.
- **Used by:** `submit_study.py`, `submit_sample.py`

---

## Webin Reports API

Used for duplicate detection before submission. Fetches records already registered under the Webin account. Implemented in `ena_common.fetch_from_reports_endpoint()`.

Auth is HTTP Basic. Responses are JSON arrays of objects with a `report` key containing entity-specific fields.

Query parameters:

| Parameter | Value |
|-----------|-------|
| `format` | `json` |
| `max-results` | configurable (default `5000`) |

### Projects (Studies)

| Environment | URL |
|-------------|-----|
| Production  | `https://www.ebi.ac.uk/ena/submit/report/projects` |
| Test        | `https://wwwdev.ebi.ac.uk/ena/submit/report/projects` |

- **Method:** `GET`
- **Used by:** `submit_study.py` (`fetch_account_studies`)

### Samples

| Environment | URL |
|-------------|-----|
| Production  | `https://www.ebi.ac.uk/ena/submit/report/samples` |
| Test        | `https://wwwdev.ebi.ac.uk/ena/submit/report/samples` |

- **Method:** `GET`
- **Used by:** `submit_sample.py` (`fetch_account_samples`)

### Runs

| Environment | URL |
|-------------|-----|
| Production  | `https://www.ebi.ac.uk/ena/submit/report/runs` |
| Test        | `https://wwwdev.ebi.ac.uk/ena/submit/report/runs` |

- **Method:** `GET`
- **Used by:** `submit_reads.py` (`fetch_account_runs`)

---

## GitHub API (webin-cli)

Used by `submit_reads.py` to discover the latest webin-cli release when `--download-webin-cli` is passed.

| Endpoint | URL |
|----------|-----|
| Latest release metadata | `https://api.github.com/repos/enasequence/webin-cli/releases/latest` |
| JAR download | `https://github.com/enasequence/webin-cli/releases/download/{version}/webin-cli-{version}.jar` |

- **Method:** `GET` (no auth required)
- **Used by:** `submit_reads.py` (`get_latest_webin_cli_version`, `_download_webin_cli`)

---

## Notes

- The **test** environment (`wwwdev.ebi.ac.uk`) discards submissions daily; use it for development and validation.
- The Reports API returns `404` when there are no records yet (treated as an empty list, not an error).
- `401` / `403` from the Reports API is logged as a warning and duplicate checking is skipped rather than aborting.
- `submit_reads.py` does not use the Webin v2 REST API for submission itself — it delegates to the **webin-cli** JAR tool instead, using the Reports API only for pre-submission duplicate detection.
