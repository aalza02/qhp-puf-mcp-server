"""Registers the four QHP PUF tools with FastMCP and serves them over Streamable HTTP.

Tool names and descriptions are written for the LLM that picks tools: each one says when to
use it, what must come before it, and what it returns. Inputs are validated with the Pydantic
models in models.py; invalid input comes back as a plain-language ToolError, never a traceback.
"""

import logging
import os
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field, ValidationError

from . import tools
from .config import load_settings
from .data_access import QhpDataAccess
from .models import (
    DEFAULT_ROW_LIMIT, MAX_ROW_LIMIT, DataRequest, Dataset, FindVariablesInput,
    PrepareDataRequestInput, PullPlanDataInput, ToolError, VerifyDataPullInput,
)

log = logging.getLogger(__name__)

SERVER_INSTRUCTIONS = (
    "Find, request, pull, and verify CMS Marketplace Qualified Health Plan (QHP) Public Use File data "
    "(Plan Attributes and Rate PUFs; public data only; all tools are read-only). Always work in this order: "
    "1) find_variables; 2) prepare_data_request, then show its plain_language_summary and questions_for_user "
    "to the user and wait for explicit approval; 3) pull_plan_data with confirmed=true and the "
    "request_fingerprint; 4) verify_data_pull. Never set confirmed=true without the user's explicit approval."
)

REQUEST_DESCRIPTION = (
    "The data request: dataset ('plan_attributes' or 'rates'), plan_years (1-3, e.g. [2026, 2027]), "
    "variables (1-25 snake_case names from find_variables), and optional filters "
    "[{variable, operator, value}]. Text operators: equals, not_equals, in. Numeric operators (only for "
    "variables marked numeric_filterable): greater_than, greater_or_equal, less_than, less_or_equal, between."
)
FINGERPRINT_DESCRIPTION = "The 64-character request_fingerprint returned by prepare_data_request."


def _invalid_input(error: ValidationError) -> dict[str, Any]:
    problems = [f"{'.'.join(str(p) for p in e['loc']) or 'input'}: {e['msg']}" for e in error.errors()]
    return ToolError(error_code="INVALID_INPUT", message="The request is not valid yet.",
                     suggestions=problems).model_dump(mode="json")


def build_server(data: QhpDataAccess | None = None) -> FastMCP:
    port = int(os.environ.get("DATABRICKS_APP_PORT", "8000"))
    mcp = FastMCP("qhp-puf", instructions=SERVER_INSTRUCTIONS, host="0.0.0.0", port=port, stateless_http=True)
    data_access = data or QhpDataAccess(load_settings())

    @mcp.tool(
        name="find_variables",
        description=(
            "STEP 1 - use first. Search the CMS QHP Public Use File variable catalog (Plan Attributes and Rate "
            "PUFs) by plain-language topic, e.g. 'deductible', 'tobacco rate', 'dental waiting period'. Returns "
            "variable names, CMS descriptions and notes, the plan years each variable exists in, any requested "
            "years where it is missing, and whether it supports numeric filters. Returns no plan data."))
    def find_variables(
        topic: Annotated[str, Field(description="Plain-language topic to search for (2-100 characters).")],
        dataset: Annotated[Dataset | None, Field(description="Optional: 'plan_attributes' or 'rates'.")] = None,
        plan_years: Annotated[list[int] | None, Field(description="Optional: years to check, e.g. [2026, 2027].")] = None,
        max_results: Annotated[int, Field(description="How many matches to return (1-50).")] = 20,
    ) -> dict[str, Any]:
        try:
            inp = FindVariablesInput(topic=topic, dataset=dataset, plan_years=plan_years, max_results=max_results)
        except ValidationError as error:
            return _invalid_input(error)
        return tools.find_variables(inp, data_access).model_dump(mode="json")

    @mcp.tool(
        name="prepare_data_request",
        description=(
            "STEP 2 - required before any pull; returns NO data. Checks a draft request: every variable and "
            "filter must exist in every requested plan year; suggests corrections for unknown names; counts "
            "matching rows; restates the request in plain language; lists questions the user must answer; and "
            "returns a request_fingerprint. Show the summary and questions to the user and wait for approval."))
    def prepare_data_request(
        request: Annotated[dict[str, Any], Field(description=REQUEST_DESCRIPTION)],
    ) -> dict[str, Any]:
        try:
            inp = PrepareDataRequestInput(request=DataRequest(**request))
        except (ValidationError, TypeError) as error:
            return _invalid_input(error) if isinstance(error, ValidationError) else ToolError(
                error_code="INVALID_INPUT", message="request must be an object.").model_dump(mode="json")
        return tools.prepare_data_request(inp, data_access).model_dump(mode="json")

    @mcp.tool(
        name="pull_plan_data",
        description=(
            "STEP 3 - only after the user explicitly approved the prepared request. Requires the identical "
            "request, confirmed=true, and the request_fingerprint from prepare_data_request. Returns up to "
            f"'limit' rows (default {DEFAULT_ROW_LIMIT}, max {MAX_ROW_LIMIT}), the total number of matching rows, "
            "and the CMS source file URL and SHA-256 fingerprint for each plan year. Read-only."))
    def pull_plan_data(
        request: Annotated[dict[str, Any], Field(description=REQUEST_DESCRIPTION)],
        confirmed: Annotated[bool, Field(description="true ONLY after the user explicitly approved the request.")],
        request_fingerprint: Annotated[str, Field(description=FINGERPRINT_DESCRIPTION)],
        limit: Annotated[int, Field(description=f"Rows to return (1-{MAX_ROW_LIMIT}).")] = DEFAULT_ROW_LIMIT,
    ) -> dict[str, Any]:
        try:
            inp = PullPlanDataInput(request=request, confirmed=confirmed,
                                    request_fingerprint=request_fingerprint, limit=limit)
        except ValidationError as error:
            return _invalid_input(error)
        return tools.pull_plan_data(inp, data_access).model_dump(mode="json")

    @mcp.tool(
        name="verify_data_pull",
        description=(
            "STEP 4 - after pull_plan_data. Independently verifies the pull: recounts matching rows and compares "
            "them with reported_matching_rows; compares loaded rows per plan year with the row counts recorded "
            "from the original CMS files; reports null counts per requested variable and rows skipped by numeric "
            "filters; and returns a SHA-256 evidence fingerprint built with SpecPack validate_pack/attest_pack."))
    def verify_data_pull(
        request: Annotated[dict[str, Any], Field(description=REQUEST_DESCRIPTION)],
        request_fingerprint: Annotated[str, Field(description=FINGERPRINT_DESCRIPTION)],
        reported_matching_rows: Annotated[int, Field(description="total_matching_rows from pull_plan_data.")],
    ) -> dict[str, Any]:
        try:
            inp = VerifyDataPullInput(request=request, request_fingerprint=request_fingerprint,
                                      reported_matching_rows=reported_matching_rows)
        except ValidationError as error:
            return _invalid_input(error)
        return tools.verify_data_pull(inp, data_access).model_dump(mode="json")

    return mcp


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    build_server().run(transport="streamable-http")  # serves the MCP endpoint at /mcp