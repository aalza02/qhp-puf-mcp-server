"""The four goal-based MCP tools: find -> prepare -> pull -> verify.

Each function takes a validated Pydantic input and returns a Pydantic result. Nothing here
raises to the caller: every failure becomes a plain-language ToolError, so the chatbot never
receives a stack trace.
"""

from __future__ import annotations

import difflib
import functools
import hashlib
import json
import logging
import re

from .data_access import DataAccessError, QhpDataAccess
from .models import (
    DEFAULT_ROW_LIMIT, MAX_ROW_LIMIT, DataFilter, DataRequest, FindVariablesInput, FindVariablesResult,
    NullCount, PrepareDataRequestInput, PreparedRequest, PullPlanDataInput, PullResult, SourceFile,
    ToolError, VariableMatch, VerificationCheck, VerificationReport, VerifyDataPullInput,
)
from .specpack import attest_pack, validate_pack
from .sql_safety import UnsafeIdentifierError

log = logging.getLogger(__name__)

DATASET_LABELS = {"plan_attributes": "Plan Attributes", "rates": "Rate"}
OPERATOR_WORDS = {
    "equals": "equals", "not_equals": "does not equal", "in": "is one of",
    "greater_than": "is greater than", "greater_or_equal": "is at least",
    "less_than": "is less than", "less_or_equal": "is at most", "between": "is between",
}
STOP_WORDS = {"the", "a", "an", "of", "for", "and", "or", "in", "on", "to", "by", "with", "plan", "plans"}
# Everyday words a user might type -> words CMS uses in variable names and descriptions.
TOPIC_SYNONYMS = {
    "premium": ["rate"], "premiums": ["rate"], "price": ["rate"], "cost": ["rate"],
    "smoker": ["tobacco"], "smoking": ["tobacco"],
    "oop": ["moop"], "out-of-pocket": ["moop"], "pocket": ["moop"],
    "insurer": ["issuer"], "company": ["issuer"],
    "tier": ["metal"], "bronze": ["metal"], "silver": ["metal"], "gold": ["metal"],
    "copay": ["copayment"], "spouse": ["couple"], "children": ["dependent", "child"], "kids": ["dependent", "child"],
}
EVIDENCE_MANIFEST = (
    'specpack_version: "0.1.0"\nname: "qhp-puf-data-pull-evidence"\nversion: "0.1.0"\n'
    'entrypoints:\n  spec: "request.json"\n  tests: "checks.json"\n'
)


def _safe(tool):
    """Turns any failure into a ToolError with a plain message; details go only to the log."""
    @functools.wraps(tool)
    def wrapper(*args, **kwargs):
        try:
            return tool(*args, **kwargs)
        except (DataAccessError, UnsafeIdentifierError) as error:
            return ToolError(error_code="REQUEST_FAILED", message=str(error))
        except Exception:
            log.exception("Unexpected error in %s", tool.__name__)
            return ToolError(error_code="INTERNAL_ERROR",
                             message="Something went wrong on the server. It was logged; please try again.")
    return wrapper


def _canonical(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _variables_by_dataset(catalog: list[dict]) -> dict[tuple[str, str], dict]:
    """One entry per (dataset, variable) with the plan years it exists in; newest year's text wins."""
    grouped: dict[tuple[str, str], dict] = {}
    for entry in catalog:
        key = (entry["dataset"], entry["variable"])
        group = grouped.setdefault(key, {**entry, "plan_years": []})
        group["plan_years"].append(entry["plan_year"])
        if entry["plan_year"] >= group["plan_year"]:
            group.update(entry)
            group["plan_years"] = grouped[key]["plan_years"]
    for group in grouped.values():
        group["plan_years"] = sorted(set(group["plan_years"]))
    return grouped


def _search_terms(topic: str) -> list[str]:
    words = [w for w in re.findall(r"[a-z0-9\-]+", topic.lower()) if w not in STOP_WORDS and len(w) > 1]
    terms = list(words)
    for word in words:
        terms += TOPIC_SYNONYMS.get(word, [])
    return list(dict.fromkeys(terms))


def _relevance(entry: dict, topic: str, terms: list[str]) -> int:
    """Name/label matches count most; description matches count a little."""
    name = f"{entry['variable'].replace('_', ' ')} {(entry['cms_variable_name'] or '').lower()}"
    label = (entry["variable_label"] or "").lower()
    description = (entry["description"] or "").lower()
    score = 10 if topic.lower().strip() in f"{name} {label}" else 0
    for term in terms:
        pattern = rf"\b{re.escape(term)}"  # 'rate' matches 'rates' but not 'integrated'
        if re.search(pattern, name) or re.search(pattern, label):
            score += 3
        elif re.search(pattern, description):
            score += 1
    return score


def _format_number(value: float) -> str:
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def _describe_filter(f: DataFilter) -> str:
    if f.operator == "in":
        value = ", ".join(f"'{v}'" for v in f.value)
    elif f.operator == "between":
        value = f"{_format_number(f.value[0])} and {_format_number(f.value[1])}"
    elif f.is_numeric:
        value = _format_number(f.value)
    else:
        value = f"'{f.value}'"
    return f"{f.variable} {OPERATOR_WORDS[f.operator]} {value}"


def _summarize(request: DataRequest, matching: dict[int, int] | None) -> str:
    years = " and ".join(str(y) for y in request.plan_years)
    text = (f"Pull {len(request.variables)} variable(s) ({', '.join(request.variables)}) from the CMS "
            f"{DATASET_LABELS[request.dataset]} Public Use File for plan year(s) {years}")
    if request.filters:
        text += ", where " + " and ".join(_describe_filter(f) for f in request.filters)
    else:
        text += ", with no filters (every row)"
    text += "."
    if any(f.is_numeric for f in request.filters):
        text += " Rows where CMS wrote text (such as 'Not Applicable') instead of a number are left out of numeric filters."
    if matching is not None:
        per_year = ", ".join(f"{y}: {n:,}" for y, n in matching.items())
        text += f" This matches {sum(matching.values()):,} rows ({per_year})."
    return text


def _review_request(request: DataRequest, data: QhpDataAccess) -> tuple[list[str], list[str]]:
    """Problems block the request; questions are things the user should consciously accept."""
    label = DATASET_LABELS[request.dataset]
    variables = {v: e for (d, v), e in _variables_by_dataset(data.variable_catalog()).items() if d == request.dataset}
    available_years = sorted({y for e in variables.values() for y in e["plan_years"]})
    problems: list[str] = []
    questions: list[str] = []

    unavailable = [y for y in request.plan_years if y not in available_years]
    if unavailable:
        problems.append(f"Plan year(s) {unavailable} are not loaded for {label}. Available: {available_years}.")
    checked_years = [y for y in request.plan_years if y in available_years]

    def check_variable(name: str, role: str) -> dict | None:
        entry = variables.get(name)
        if entry is None:
            close = difflib.get_close_matches(name, list(variables), n=3, cutoff=0.6)
            hint = f" Did you mean: {', '.join(close)}?" if close else " Use find_variables to look it up."
            problems.append(f"{role} '{name}' is not a {label} variable.{hint}")
            return None
        absent = [y for y in checked_years if y not in entry["plan_years"]]
        if absent:
            problems.append(f"{role} '{name}' does not exist in plan year(s) {absent}. Remove it or drop those years.")
        return entry

    for name in request.variables:
        entry = check_variable(name, "Variable")
        if entry and entry["notes"]:
            questions.append(f"CMS note on '{name}': {entry['notes']} Is that acceptable?")
    for f in request.filters:
        entry = check_variable(f.variable, "Filter variable")
        if entry and f.is_numeric and not entry["is_numeric"]:
            problems.append(f"'{f.variable}' holds text, so '{f.operator}' can't be used. Use equals, not_equals, or in.")
    return problems, questions


@_safe
def find_variables(inp: FindVariablesInput, data: QhpDataAccess) -> FindVariablesResult | ToolError:
    terms = _search_terms(inp.topic)
    if not terms:
        return ToolError(error_code="EMPTY_TOPIC", message="Please describe a topic, e.g. 'deductible' or 'tobacco rate'.")
    scored = []
    for (dataset, _), entry in _variables_by_dataset(data.variable_catalog()).items():
        if inp.dataset and dataset != inp.dataset:
            continue
        score = _relevance(entry, inp.topic, terms)
        if score > 0:
            scored.append((score, entry))
    if not scored:
        return ToolError(error_code="NO_MATCHES", message=f"No variables match '{inp.topic}'.",
                         suggestions=["Try a broader word such as 'deductible', 'rate', 'metal', or 'issuer'."])
    scored.sort(key=lambda pair: (-pair[0], pair[1]["dataset"], pair[1]["variable"]))
    matches = [
        VariableMatch(
            dataset=e["dataset"], variable=e["variable"], label=e["variable_label"], description=e["description"],
            notes=e["notes"], plan_years=e["plan_years"],
            missing_in_requested_years=[y for y in (inp.plan_years or []) if y not in e["plan_years"]],
            numeric_filterable=bool(e["is_numeric"]), unit=e["unit"])
        for _, e in scored[: inp.max_results]
    ]
    return FindVariablesResult(
        topic=inp.topic, matches=matches,
        next_step="Choose variables and plan years, then call prepare_data_request. It returns no data.")


@_safe
def prepare_data_request(inp: PrepareDataRequestInput, data: QhpDataAccess) -> PreparedRequest | ToolError:
    request = inp.request
    problems, questions = _review_request(request, data)
    matching = None
    if not problems:
        matching = data.count_matching_rows(request)
        total = sum(matching.values())
        if total == 0:
            questions.insert(0, "No rows match these filters. Check filter values (state codes look like 'TX').")
        elif total > DEFAULT_ROW_LIMIT:
            questions.insert(0, f"{total:,} rows match. pull_plan_data returns the first {DEFAULT_ROW_LIMIT} by default "
                                f"(up to {MAX_ROW_LIMIT}), sorted by plan year and the requested variables. "
                                f"Is that enough, or should filters be added?")
    questions.append("Do you approve this exact request? Reply yes to pull the data.")
    if problems:
        next_step = "Fix the problems listed, then call prepare_data_request again."
    else:
        next_step = ("Show the summary and questions to the user. Only after they approve, call pull_plan_data "
                     "with the same request, confirmed=true, and this request_fingerprint.")
    return PreparedRequest(
        status="needs_answers" if problems else "ready_for_confirmation", request=request,
        request_fingerprint=request.fingerprint(), plain_language_summary=_summarize(request, matching),
        problems=problems, questions_for_user=questions, next_step=next_step)


@_safe
def pull_plan_data(inp: PullPlanDataInput, data: QhpDataAccess) -> PullResult | ToolError:
    request = inp.request  # confirmed=true and an unchanged fingerprint were enforced by the input model
    problems, _ = _review_request(request, data)
    if problems:
        return ToolError(error_code="REQUEST_HAS_PROBLEMS", message="This request can't be pulled yet.",
                         suggestions=problems)
    matching = data.count_matching_rows(request)
    result = data.fetch_rows(request, inp.limit)
    total = sum(matching.values())
    sources = [SourceFile(plan_year=m["plan_year"], source_url=m["source_url"], file_sha256=m["file_sha256"],
                          cms_row_count=m["row_count"])
               for m in data.source_manifest(request.dataset, request.plan_years)]
    return PullResult(
        request_fingerprint=request.fingerprint(), total_matching_rows=total, returned_rows=len(result.rows),
        truncated=len(result.rows) < total, columns=result.columns, rows=result.rows, sources=sources,
        next_step=(f"To verify, call verify_data_pull with the same request, this request_fingerprint, "
                   f"and reported_matching_rows={total}."))


@_safe
def verify_data_pull(inp: VerifyDataPullInput, data: QhpDataAccess) -> VerificationReport | ToolError:
    request = inp.request
    if inp.request_fingerprint != request.fingerprint():
        return ToolError(error_code="FINGERPRINT_MISMATCH",
                         message="The request does not match its fingerprint. Verify the exact request that was pulled.")
    checks: list[VerificationCheck] = []

    recount = sum(data.count_matching_rows(request).values())
    checks.append(VerificationCheck(
        name="matching_row_count", passed=recount == inp.reported_matching_rows,
        expected=str(inp.reported_matching_rows), actual=str(recount),
        details="Fresh recount of rows meeting the request, compared with the count pull_plan_data reported."))

    manifest = {m["plan_year"]: m for m in data.source_manifest(request.dataset, request.plan_years)}
    loaded = data.loaded_row_counts(request.dataset, request.plan_years)
    for year in request.plan_years:
        cms_rows = manifest.get(year, {}).get("row_count")
        sha = manifest.get(year, {}).get("file_sha256", "unknown")
        checks.append(VerificationCheck(
            name=f"loaded_rows_match_cms_file_{year}", passed=cms_rows is not None and cms_rows == loaded.get(year, 0),
            expected=str(cms_rows), actual=str(loaded.get(year, 0)),
            details=f"Rows loaded for {year} vs rows counted in the original CMS file (SHA-256 {sha[:12]}...)."))

    for skipped in data.non_numeric_excluded(request):
        checks.append(VerificationCheck(
            name=f"non_numeric_skipped_{skipped['variable']}_{skipped['plan_year']}", passed=True,
            actual=str(skipped["excluded_rows"]),
            details=(f"Informational: rows left out of the numeric filter on {skipped['variable']} because CMS "
                     f"wrote text (e.g. 'Not Applicable') instead of a number.")))

    null_counts = [NullCount(**n) for n in data.null_counts(request)]

    # Bundle the evidence as a SpecPack, validate it, and fingerprint every file.
    evidence = {
        "specpack.yaml": EVIDENCE_MANIFEST,
        "request.json": request.canonical_json(),
        "checks.json": _canonical([c.model_dump() for c in checks]),
        "null_counts.json": _canonical([n.model_dump() for n in null_counts]),
        "source_manifest.json": _canonical([manifest[y] for y in sorted(manifest)]),
    }
    validation = validate_pack(evidence)
    lock = attest_pack(evidence)
    evidence_fingerprint = hashlib.sha256(_canonical(lock).encode("utf-8")).hexdigest()

    passed = sum(c.passed for c in checks)
    all_passed = passed == len(checks) and validation["valid"]
    return VerificationReport(
        request_fingerprint=request.fingerprint(), all_checks_passed=all_passed,
        summary=(f"{passed} of {len(checks)} checks passed; evidence pack "
                 f"{'valid' if validation['valid'] else 'INVALID'}; evidence fingerprint {evidence_fingerprint[:12]}..."),
        checks=checks, null_counts=null_counts, evidence_pack_valid=validation["valid"],
        evidence_pack_errors=validation["errors"], evidence_files=lock["files"],
        evidence_fingerprint=evidence_fingerprint)