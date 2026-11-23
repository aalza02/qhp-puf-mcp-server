"""Evaluation suite for the QHP PUF MCP server.

Runs fixed test inputs against the real tools and data and checks each result against
expected values computed independently with separate SQL on the Delta tables.
Produces a Markdown results table for docs/EVALUATION.md.

From a Databricks notebook (with the repo on sys.path and settings in environment variables):
    from evaluation.run_evaluation import run_evaluation, results_markdown
    results = await run_evaluation()
    print(results_markdown(results))
"""

from __future__ import annotations

import inspect
import json
import statistics
import time
from dataclasses import dataclass

from pydantic import ValidationError

from qhp_mcp.config import load_settings
from qhp_mcp.data_access import QhpDataAccess
from qhp_mcp.models import (DataRequest, FindVariablesInput, PrepareDataRequestInput,
                            PullPlanDataInput, VerifyDataPullInput)
from qhp_mcp.server import build_server
from qhp_mcp.tools import find_variables, prepare_data_request, pull_plan_data, verify_data_pull

# Expected values computed independently with direct SQL on the tables (not with these tools).
TX_RATE_UNDER_500 = {2026: 112_071, 2027: 110_814}
TX_TOBACCO_PRICED = {2026: 100_521, 2027: 114_291}
ALL_RATES_ROWS = {2026: 2_235_761, 2027: 2_115_809}
TOBACCO_RATE_NULLS = {2026: 1_138_292, 2027: 1_026_857}

TX = {"variable": "state_code", "operator": "equals", "value": "TX"}
REQ_TX_UNDER_500 = {"dataset": "rates", "plan_years": [2026, 2027],
                    "variables": ["plan_id", "state_code", "individual_rate"],
                    "filters": [TX, {"variable": "individual_rate", "operator": "less_than", "value": 500}]}
REQ_TX_TOBACCO = {"dataset": "rates", "plan_years": [2026, 2027],
                  "variables": ["plan_id", "age", "individual_tobacco_rate"],
                  "filters": [TX, {"variable": "tobacco", "operator": "equals",
                                   "value": "Tobacco User/Non-Tobacco User"}]}
REQ_ALL_RATES = {"dataset": "rates", "plan_years": [2026, 2027],
                 "variables": ["plan_id", "individual_tobacco_rate"]}


@dataclass
class CaseResult:
    case_id: str
    category: str
    description: str
    passed: bool
    detail: str
    seconds: float


def _fingerprint(request: dict) -> str:
    return DataRequest(**request).fingerprint()


def _prepare(request: dict, data: QhpDataAccess):
    return prepare_data_request(PrepareDataRequestInput(request=request), data)


def _pull(request: dict, data: QhpDataAccess, limit: int = 100):
    return pull_plan_data(PullPlanDataInput(request=request, confirmed=True,
                                            request_fingerprint=_fingerprint(request), limit=limit), data)


def _verify(request: dict, data: QhpDataAccess, reported: int):
    return verify_data_pull(VerifyDataPullInput(request=request, request_fingerprint=_fingerprint(request),
                                                reported_matching_rows=reported), data)


def _rejected(build) -> tuple[bool, str]:
    try:
        build()
        return False, "accepted, but should have been rejected"
    except ValidationError as error:
        return True, error.errors()[0]["msg"][:120]


def _problems_contain(request: dict, data: QhpDataAccess, text: str) -> tuple[bool, str]:
    problems = _prepare(request, data).problems
    return any(text in p for p in problems), (problems[0][:150] if problems else "no problems reported")


def _mcp_text(result) -> str:
    content = result[0] if isinstance(result, tuple) else result
    return "".join(getattr(part, "text", "") for part in content)


def build_cases(data: QhpDataAccess, server) -> list[tuple]:
    cases: list[tuple] = []

    def case(case_id: str, category: str, description: str):
        def register(fn):
            cases.append((case_id, category, description, fn))
            return fn
        return register

    # ---------- A. Accuracy against independent SQL ----------
    @case("A1", "Accuracy", "TX rates under $500: matching rows equal independent SQL")
    def _():
        pulled = _pull(REQ_TX_UNDER_500, data)
        return pulled.total_matching_rows == sum(TX_RATE_UNDER_500.values()), f"{pulled.total_matching_rows:,} rows"

    @case("A2", "Accuracy", "TX tobacco-priced rates: matching rows equal independent SQL")
    def _():
        pulled = _pull(REQ_TX_TOBACCO, data)
        return pulled.total_matching_rows == sum(TX_TOBACCO_PRICED.values()), f"{pulled.total_matching_rows:,} rows"

    @case("A3", "Accuracy", "No filters: rows and source row counts equal the CMS files")
    def _():
        pulled = _pull(REQ_ALL_RATES, data)
        sources = {s.plan_year: s.cms_row_count for s in pulled.sources}
        ok = pulled.total_matching_rows == sum(ALL_RATES_ROWS.values()) and sources == ALL_RATES_ROWS
        return ok, f"{pulled.total_matching_rows:,} rows; CMS files {sources}"

    @case("A4", "Accuracy", "Every returned row meets the filters (TX and rate < 500)")
    def _():
        rows = _pull(REQ_TX_UNDER_500, data, limit=1000).rows
        ok = bool(rows) and all(r["state_code"] == "TX" and float(r["individual_rate"]) < 500 for r in rows)
        return ok, f"checked {len(rows)} rows"

    @case("A5", "Accuracy", "Null counts for individual_tobacco_rate equal independent SQL")
    def _():
        report = _verify(REQ_ALL_RATES, data, sum(ALL_RATES_ROWS.values()))
        nulls = {n.plan_year: n.null_rows for n in report.null_counts if n.variable == "individual_tobacco_rate"}
        return nulls == TOBACCO_RATE_NULLS, f"nulls {nulls}"

    @case("A6", "Accuracy", "verify_data_pull passes all checks for a correct pull")
    def _():
        report = _verify(REQ_TX_UNDER_500, data, sum(TX_RATE_UNDER_500.values()))
        return report.all_checks_passed and report.evidence_pack_valid, report.summary

    # ---------- V. Validation, safety, graceful errors ----------
    @case("V1", "Safety", "SQL-injection variable name is rejected before any query")
    def _():
        return _rejected(lambda: DataRequest(dataset="rates", plan_years=[2027], variables=["plan_id; DROP TABLE rates"]))

    @case("V2", "Safety", "Unknown dataset is rejected")
    def _():
        return _rejected(lambda: DataRequest(dataset="claims", plan_years=[2027], variables=["plan_id"]))

    @case("V3", "Safety", "Row limit above 1000 is rejected")
    def _():
        return _rejected(lambda: PullPlanDataInput(request=REQ_TX_TOBACCO, confirmed=True,
                                                   request_fingerprint=_fingerprint(REQ_TX_TOBACCO), limit=5000))

    @case("V4", "Safety", "Pull without user confirmation is rejected")
    def _():
        return _rejected(lambda: PullPlanDataInput(request=REQ_TX_TOBACCO, confirmed=False,
                                                   request_fingerprint=_fingerprint(REQ_TX_TOBACCO)))

    @case("V5", "Safety", "Pull of a request changed after preparation is rejected")
    def _():
        return _rejected(lambda: PullPlanDataInput(request=REQ_TX_TOBACCO, confirmed=True,
                                                   request_fingerprint=_fingerprint(REQ_TX_UNDER_500)))

    @case("V6", "Validation", "Misspelled variable gets a 'Did you mean' suggestion")
    def _():
        return _problems_contain({"dataset": "rates", "plan_years": [2027], "variables": ["individual_rates"]},
                                 data, "Did you mean")

    @case("V7", "Validation", "Variable missing in a requested year is flagged (2027-only field in 2026)")
    def _():
        return _problems_contain({"dataset": "plan_attributes", "plan_years": [2026, 2027],
                                  "variables": ["plan_id", "adult_dental_services_waiting_period"]},
                                 data, "does not exist in plan year(s) [2026]")

    @case("V8", "Validation", "Filter value that never occurs is flagged with the valid values")
    def _():
        return _problems_contain({**REQ_TX_TOBACCO, "filters": [TX, {"variable": "tobacco", "operator": "equals",
                                                                    "value": "Tobacco User"}]},
                                 data, "Valid values")

    @case("V9", "Validation", "Numeric operator on a text variable is flagged")
    def _():
        return _problems_contain({"dataset": "rates", "plan_years": [2027], "variables": ["plan_id"],
                                  "filters": [{"variable": "state_code", "operator": "greater_than", "value": 5}]},
                                 data, "holds text")

    @case("V10", "Validation", "Plan year that is not loaded is flagged")
    def _():
        return _problems_contain({"dataset": "rates", "plan_years": [2019], "variables": ["plan_id"]},
                                 data, "not loaded")

    @case("V11", "Validation", "Verification fails when the reported row count is wrong")
    def _():
        report = _verify(REQ_TX_UNDER_500, data, 999)
        failed = [c.name for c in report.checks if not c.passed]
        return (not report.all_checks_passed) and failed == ["matching_row_count"], f"failed checks: {failed}"

    @case("V12", "Safety", "Through MCP, an unapproved pull returns a clean error with no traceback")
    async def _():
        text = _mcp_text(await server.call_tool("pull_plan_data", {
            "request": REQ_TX_TOBACCO, "confirmed": False, "request_fingerprint": _fingerprint(REQ_TX_TOBACCO)}))
        payload = json.loads(text)
        ok = payload.get("ok") is False and payload.get("error_code") == "INVALID_INPUT" and "Traceback" not in text
        return ok, f"{payload.get('error_code')}: {payload.get('message')}"

    # ---------- T. Tool design (helps an LLM pick and use the right tool) ----------
    @case("T1", "Tool design", "find_variables('tobacco premium') ranks individual_tobacco_rate first")
    def _():
        top = find_variables(FindVariablesInput(topic="tobacco premium"), data).matches[0]
        return top.variable == "individual_tobacco_rate", f"top match: {top.dataset}.{top.variable}"

    @case("T2", "Tool design", "find_variables flags the dental waiting period as missing in 2026")
    def _():
        matches = find_variables(FindVariablesInput(topic="dental waiting period", plan_years=[2026, 2027]), data).matches
        target = next((m for m in matches if m.variable == "adult_dental_services_waiting_period"), None)
        return bool(target) and target.missing_in_requested_years == [2026], f"missing: {target and target.missing_in_requested_years}"

    @case("T3", "Tool design", "find_variables limited to plan_attributes returns only that dataset")
    def _():
        matches = find_variables(FindVariablesInput(topic="deductible", dataset="plan_attributes"), data).matches
        return bool(matches) and all(m.dataset == "plan_attributes" for m in matches), f"{len(matches)} matches"

    @case("T4", "Tool design", "Nonsense topic returns a helpful NO_MATCHES error")
    def _():
        result = find_variables(FindVariablesInput(topic="xyzzy"), data)
        return getattr(result, "error_code", None) == "NO_MATCHES", getattr(result, "message", "")

    @case("T5", "Tool design", "All 4 tools are registered over MCP with step-labelled descriptions")
    async def _():
        listed = await server.list_tools()
        names = [t.name for t in listed]
        expected = ["find_variables", "prepare_data_request", "pull_plan_data", "verify_data_pull"]
        ok = names == expected and all((t.description or "").startswith("STEP") for t in listed)
        return ok, ", ".join(names)

    # ---------- R. Reliability and determinism ----------
    @case("R1", "Reliability", "Row cap: limit=1000 returns exactly 1000 rows and marks truncation")
    def _():
        pulled = _pull(REQ_TX_TOBACCO, data, limit=1000)
        return pulled.returned_rows == 1000 and pulled.truncated, f"returned {pulled.returned_rows}, truncated {pulled.truncated}"

    @case("R2", "Reliability", "Repeated pulls of the same request return identical rows")
    def _():
        first, second = _pull(REQ_TX_TOBACCO, data, limit=50), _pull(REQ_TX_TOBACCO, data, limit=50)
        return first.rows == second.rows, f"{len(first.rows)} rows compared"

    @case("R3", "Reliability", "Repeated verification produces the same evidence fingerprint")
    def _():
        total = sum(TX_TOBACCO_PRICED.values())
        a, b = _verify(REQ_TX_TOBACCO, data, total), _verify(REQ_TX_TOBACCO, data, total)
        return a.evidence_fingerprint == b.evidence_fingerprint, a.evidence_fingerprint[:16] + "..."

    @case("R4", "Reliability", "prepare_data_request returns no plan data")
    def _():
        dumped = _prepare(REQ_TX_TOBACCO, data).model_dump()
        return "rows" not in dumped and dumped["status"] == "ready_for_confirmation", dumped["status"]

    return cases


async def run_evaluation() -> list[CaseResult]:
    data = QhpDataAccess(load_settings())
    server = build_server(data)
    results = []
    for case_id, category, description, fn in build_cases(data, server):
        start = time.perf_counter()
        try:
            outcome = fn()
            if inspect.isawaitable(outcome):
                outcome = await outcome
            passed, detail = outcome
        except Exception as error:  # a crash is recorded as a failure, never hidden
            passed, detail = False, f"raised {type(error).__name__}: {error}"[:200]
        results.append(CaseResult(case_id, category, description, bool(passed), str(detail),
                                  time.perf_counter() - start))
    return results


def results_markdown(results: list[CaseResult]) -> str:
    passed = sum(r.passed for r in results)
    lines = [f"**{passed} of {len(results)} evaluation cases passed.**", "",
             "| ID | Category | Test | Result | Time (s) | Detail |", "|---|---|---|---|---|---|"]
    for r in results:
        detail = r.detail.replace("|", "/").replace("\n", " ")
        lines.append(f"| {r.case_id} | {r.category} | {r.description} | {'PASS' if r.passed else 'FAIL'} "
                     f"| {r.seconds:.2f} | {detail} |")
    times = [r.seconds for r in results]
    lines += ["", f"Median case time: {statistics.median(times):.2f} s; slowest case: {max(times):.2f} s."]
    return "\n".join(lines)