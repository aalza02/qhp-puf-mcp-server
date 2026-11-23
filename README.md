# QHP PUF MCP Server

A read-only **Model Context Protocol (MCP) server**, hosted on Databricks, that lets a chatbot find, request,
pull, and **verify** variables from the CMS Marketplace **Qualified Health Plan (QHP) Public Use Files**
(Plan Attributes and Rate PUFs) across multiple plan years.

Source data (public only): https://www.cms.gov/marketplace/resources/data/public-use-files

## Why
Analysts and oversight staff often need specific QHP variables across several plan years. This server turns data requests into a guided, verifiable conversation: the user describes what they need in plain language,
approves an exact request, and receives the data **with evidence that it matches the original CMS files**.

## The four tools (each one is a user goal)
| Step | Tool | What it does | Returns data? |
|---|---|---|---|
| 1 | `find_variables` | Searches the variable catalog by topic (understands everyday words like "premium" or "smoker"); shows which plan years each variable exists in and whether it supports numeric filters | No |
| 2 | `prepare_data_request` | Validates a draft request; checks every variable and filter exists in every requested year; rejects filter values that never occur and lists valid ones; counts matching rows; restates the request in plain language; lists questions the user must answer; returns a `request_fingerprint` | **No** |
| 3 | `pull_plan_data` | Only for an approved request: requires `confirmed=true` **and** the unchanged `request_fingerprint`. Returns rows (default 100, max 1000), total matching rows, and each year's CMS source URL and file SHA-256 | Yes |
| 4 | `verify_data_pull` | Independent checks: fresh row recount vs. reported; loaded rows per year vs. the row count recorded from the original CMS file; null counts per variable; rows skipped by numeric filters; a SHA-256 **evidence fingerprint** built with SpecPack `validate_pack` / `attest_pack` | Evidence only |

## Repository layout
```
qhp_mcp/
  config.py        settings from environment variables (no workspace names or secrets in code)
  models.py        Pydantic v2 models for every tool input and output
  sql_safety.py    column allow-list + ^[a-z][a-z0-9_]*$ check; all values become SQL parameters
  data_access.py   the only module that queries data (read-only SELECTs, parameterized)
  tools.py         the four goal-based tools; every failure becomes a plain ToolError
  server.py        FastMCP registration and Streamable HTTP server (/mcp)
  specpack/        SpecPack v0.1.0 validate_pack / attest_pack (pure, deterministic)
app.py, app.yaml.example, requirements.txt   Databricks App entry point and config template
data_pipeline/     notebooks that download, fingerprint, load, and catalog the CMS files
demo/chat_demo.py  chatbot demo: a Databricks-hosted LLM using the tools over MCP
evaluation/        automated evaluation suite (27 cases)
tests/specpack/    official SpecPack conformance tests
docs/EVALUATION.md methodology, results, security, lessons learned
```

## Data design (Unity Catalog)
| Table | Contents |
|---|---|
| `plan_attributes`, `rates` | CMS columns in snake_case, exact CMS text, plus `plan_year`, `source_file`, and numeric `<variable>_num` twins for numeric filtering |
| `variable_catalog` | One row per (dataset, variable, plan year) with CMS data-dictionary description, label, notes, numeric flag |
| `source_manifest` | Per file: CMS source URL, SHA-256 of the downloaded file, and row count counted from the original CSV |

Plan years loaded: **2026 and 2027** (Plan Attributes: 22,059 / 20,062 rows; Rates: 2,235,761 / 2,115,809 rows).
Loaded row counts match the original CMS files exactly.

## Run it
1. Run the notebooks in `data_pipeline/` in order (01 → 03) with your own catalog and schema.
2. Grant the app's service principal `USE SCHEMA` and `SELECT` on the four tables only.
3. Copy `app.yaml.example` to `app.yaml`, set your catalog and schema, and add a SQL warehouse app resource with key `sql-warehouse`.
4. Create a Databricks App (name starting with `mcp-`), deploy this folder; the MCP endpoint is `<app URL>/mcp`.
5. Run the evaluation or demo from a notebook (see `evaluation/run_evaluation.py` and `demo/chat_demo.py`).

## Security
- Public data only; no PII. No tokens, workspace URLs, or IDs in the repository (`app.yaml` is git-ignored).
- Read-only: every query is a `SELECT`; the app's identity has `SELECT` on four tables and nothing else.
- Values are always SQL parameters; column names must pass a pattern check **and** the catalog allow-list.
- Row cap of 1000; plain-language errors; stack traces go to server logs only.
- Deployed behind the workspace's Private Link with OAuth-only access.

## Evaluation
27 of 27 automated cases pass (accuracy vs. independent SQL, safety, tool design, reliability), plus 4 of 4 SpecPack
conformance tests. Details: [docs/EVALUATION.md](docs/EVALUATION.md).

## License
MIT