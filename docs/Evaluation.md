# Evaluation

## 1. Testing methodology
All tests run against the **real tools and the real data** (CMS QHP PUFs, plan years 2026 and 2027).
Expected values were computed **independently** with direct SQL on the Delta tables, not with the server's code.

| Layer | How it is tested |
|---|---|
| Data pipeline | Row count of every CMS CSV recorded at download time (`source_manifest`); loader refuses to save if loaded rows differ |
| Input models | Pydantic validation tests (unsafe names, bad datasets, limits, confirmation, fingerprint) |
| Tools | `evaluation/run_evaluation.py`: 27 fixed cases in four categories |
| MCP layer | Tool listing and tool calls through FastMCP (`list_tools`, `call_tool`) |
| SpecPack | `tests/specpack/test_specpack.py` runs the official `tests.yaml` unchanged |
| End to end | Two-turn chatbot demo (Databricks-hosted Llama 3.3 70B) calling the tools over MCP |

Environment: Databricks serverless, Python 3.12.3, mcp 1.30.0, pydantic 2.13.3, databricks-sdk 0.122.0, pyyaml 6.0.3.

## 2. Results
**27 of 27 evaluation cases passed. 4 of 4 SpecPack conformance tests passed.**

| ID | Category | Test | Result | Detail |
|---|---|---|---|---|
| A1 | Accuracy | TX rates under $500: matching rows equal independent SQL | PASS | 222,885 rows |
| A2 | Accuracy | TX tobacco-priced rates: matching rows equal independent SQL | PASS | 214,812 rows |
| A3 | Accuracy | No filters: rows and source row counts equal the CMS files | PASS | 4,351,570 rows |
| A4 | Accuracy | Every returned row meets the filters (TX and rate < 500) | PASS | 1,000 rows checked |
| A5 | Accuracy | Null counts for individual_tobacco_rate equal independent SQL | PASS | 1,138,292 / 1,026,857 |
| A6 | Accuracy | verify_data_pull passes all checks for a correct pull | PASS | 5 of 5 checks; evidence pack valid |
| V1 | Safety | SQL-injection variable name rejected before any query | PASS | pattern check |
| V2 | Safety | Unknown dataset rejected | PASS | |
| V3 | Safety | Row limit above 1000 rejected | PASS | |
| V4 | Safety | Pull without user confirmation rejected | PASS | |
| V5 | Safety | Pull of a request changed after preparation rejected | PASS | fingerprint mismatch |
| V6 | Validation | Misspelled variable gets a "Did you mean" suggestion | PASS | |
| V7 | Validation | Variable missing in a requested year flagged | PASS | 2027-only field in 2026 |
| V8 | Validation | Filter value that never occurs flagged with valid values | PASS | |
| V9 | Validation | Numeric operator on a text variable flagged | PASS | |
| V10 | Validation | Plan year not loaded flagged | PASS | |
| V11 | Validation | Verification fails when the reported row count is wrong | PASS | only `matching_row_count` fails |
| V12 | Safety | Through MCP, unapproved pull returns clean error, no traceback | PASS | INVALID_INPUT |
| T1 | Tool design | "tobacco premium" ranks individual_tobacco_rate first | PASS | |
| T2 | Tool design | Dental waiting period flagged as missing in 2026 | PASS | |
| T3 | Tool design | Dataset filter returns only that dataset | PASS | |
| T4 | Tool design | Nonsense topic returns helpful NO_MATCHES error | PASS | |
| T5 | Tool design | 4 tools registered over MCP with step-labelled descriptions | PASS | |
| R1 | Reliability | Row cap: limit=1000 returns exactly 1000 rows, marks truncation | PASS | |
| R2 | Reliability | Repeated pulls return identical rows | PASS | deterministic ordering |
| R3 | Reliability | Repeated verification gives the same evidence fingerprint | PASS | reproducible evidence |
| R4 | Reliability | prepare_data_request returns no plan data | PASS | |

**Performance:** database-backed cases took 0.9–4.6 s each (slowest: two consecutive 50-row pulls, 4.62 s);
validation cases are effectively instant because invalid input is rejected before any query.

**End-to-end demo:** asked for Texas premiums for tobacco users in 2026–2027, the chatbot found the variables,
prepared a request (`state_code = 'TX'`, `tobacco = 'Tobacco User/Non-Tobacco User'`, 214,812 rows), stopped for
approval, then pulled and verified the data (all checks passed, evidence pack valid).

## 3. Security considerations
- Public CMS data only; no PII; no credentials, workspace URLs, or IDs in the repository.
- Read-only by design and by permission: SELECT-only queries; the app's service principal holds `SELECT` on four tables only.
- SQL injection: values are bound parameters; column names must match `^[a-z][a-z0-9_]*$` and the catalog allow-list.
- Human in the loop: pulls require explicit confirmation **and** an unchanged request fingerprint.
- Errors: plain-language `ToolError`s; technical details only in server logs.
- Network: deployed on Databricks Apps behind the workspace's Private Link; OAuth-only access.

## 4. Lessons learned
1. **Guessed filter values silently return zero rows.** The demo chatbot first filtered `tobacco = 'Tobacco User'`, which never occurs. We added a check that rejects such values and lists the valid ones; the chatbot now self-corrects.
2. **LLMs can drop conditions.** The chatbot once ignored "Texas". The plain-language summary made this visible before approval; we also tightened the chatbot's instructions.
3. **Variables change between years.** Two Plan Attributes fields exist only in 2027; the catalog records availability per year so requests are checked before any pull.
4. **CMS numbers are text** (`$2,900 `, `73.22%`, `Not Applicable`). We keep exact CMS text for fidelity and add numeric twins for filtering, and report rows skipped as non-numeric.
5. **Library versions matter.** MCP SDK 2.x renamed FastMCP; we pinned the tested 1.x version.
6. **Enterprise networks restrict access.** The deployed app requires OAuth and private network access, so the demo uses the same server code over MCP's in-memory transport.

## 5. Recommendations
- Add Benefits & Cost Sharing (files already downloaded and fingerprinted) and more plan years.
- Register the server with the organization's approved MCP client using OAuth so analysts can use it from their chatbot.
- Publish the evidence pack with each pull so reviewers can re-verify results independently.