"""Pydantic v2 models for MCP tool inputs and outputs.

Every request from the chatbot is validated here before any database work happens, so a
malformed request fails fast with a clear, fixable message instead of reaching SQL.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Annotated, Literal

from pydantic import (BaseModel, BeforeValidator, ConfigDict, Field, StringConstraints,
                      field_validator, model_validator)

SAFE_IDENTIFIER_PATTERN = r"^[a-z][a-z0-9_]*$"
FINGERPRINT_PATTERN = r"^[0-9a-f]{64}$"
DEFAULT_ROW_LIMIT = 100
MAX_ROW_LIMIT = 1000
MAX_PLAN_YEARS = 3
MAX_VARIABLES = 25
MAX_FILTERS = 10
MAX_IN_VALUES = 100

Dataset = Literal["plan_attributes", "rates"]

PlanYear = Annotated[int, Field(ge=2014, le=2100, description="Plan (business) year, e.g. 2027.")]

def _tidy_variable_name(raw: object) -> object:
    """Trims spaces and lowercases BEFORE the safety pattern is checked ('Plan_Id ' -> 'plan_id')."""
    return raw.strip().lower() if isinstance(raw, str) else raw


# Order matters: the BeforeValidator listed last runs first, then the length and pattern checks.
VariableName = Annotated[
    str,
    StringConstraints(max_length=80, pattern=SAFE_IDENTIFIER_PATTERN),
    BeforeValidator(_tidy_variable_name),
    Field(description="snake_case variable name exactly as returned by find_variables, e.g. 'metal_level'."),
]

TEXT_OPERATORS = ("equals", "not_equals", "in")
NUMERIC_OPERATORS = ("greater_than", "greater_or_equal", "less_than", "less_or_equal", "between")
FilterOperator = Literal["equals", "not_equals", "in",
                         "greater_than", "greater_or_equal", "less_than", "less_or_equal", "between"]


def _as_number(raw: str | float) -> float:
    """Accepts 500, '500', or '$1,500' (how CMS writes money); rejects anything else."""
    if isinstance(raw, bool):
        raise ValueError("true/false is not a number")
    if isinstance(raw, (int, float)):
        number = float(raw)
    else:
        try:
            number = float(re.sub(r"[$,%\s]", "", raw))
        except ValueError:
            raise ValueError(f"'{raw}' is not a number") from None
    if not math.isfinite(number):
        raise ValueError("number must be finite")
    return number


def _as_text(raw: str | float) -> str:
    """Text comparisons use CMS's exact text, so 40 becomes '40', not '40.0'."""
    if isinstance(raw, float) and raw.is_integer():
        return str(int(raw))
    return str(raw).strip()


class DataFilter(BaseModel):
    """One condition rows must meet, e.g. state_code equals 'TX' or individual_rate less_than 500."""

    model_config = ConfigDict(extra="forbid")

    variable: VariableName
    operator: FilterOperator = Field(
        description="Text: equals, not_equals, in. Numeric (only for variables find_variables marks "
                    "numeric_filterable): greater_than, greater_or_equal, less_than, less_or_equal, between.")
    value: str | float | list[str | float] = Field(
        description="A single value; a list for 'in'; [low, high] for 'between'. Money may be written as '$500'.")

    @model_validator(mode="after")
    def normalize_value_for_operator(self) -> "DataFilter":
        if self.operator == "in":
            if not isinstance(self.value, list) or not 1 <= len(self.value) <= MAX_IN_VALUES:
                raise ValueError(f"'in' needs a list of 1 to {MAX_IN_VALUES} values")
            self.value = list(dict.fromkeys(_as_text(v) for v in self.value))
        elif self.operator == "between":
            if not isinstance(self.value, list) or len(self.value) != 2:
                raise ValueError("'between' needs exactly two numbers: [low, high]")
            low, high = (_as_number(v) for v in self.value)
            if low > high:
                raise ValueError("'between' needs low <= high")
            self.value = [low, high]
        elif isinstance(self.value, list):
            raise ValueError(f"'{self.operator}' needs a single value, not a list")
        elif self.operator in NUMERIC_OPERATORS:
            self.value = _as_number(self.value)
        else:
            self.value = _as_text(self.value)
        return self

    @property
    def is_numeric(self) -> bool:
        return self.operator in NUMERIC_OPERATORS


class DataRequest(BaseModel):
    """What the user wants: one dataset, 1-3 plan years, the variables to return, optional filters."""

    model_config = ConfigDict(extra="forbid")

    dataset: Dataset = Field(description="'plan_attributes' (plan design, cost sharing) or 'rates' (premiums).")
    plan_years: list[PlanYear] = Field(min_length=1, max_length=MAX_PLAN_YEARS)
    variables: list[VariableName] = Field(min_length=1, max_length=MAX_VARIABLES)
    filters: list[DataFilter] = Field(default_factory=list, max_length=MAX_FILTERS)

    @field_validator("plan_years")
    @classmethod
    def sort_unique_years(cls, years: list[int]) -> list[int]:
        return sorted(set(years))

    @field_validator("variables")
    @classmethod
    def unique_variables_in_order(cls, variables: list[str]) -> list[str]:
        return list(dict.fromkeys(variables))

    def canonical_json(self) -> str:
        """One stable text form of the request, so identical requests always hash the same."""
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))

    def fingerprint(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


class FindVariablesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic: str = Field(min_length=2, max_length=100,
                       description="Plain-language topic, e.g. 'deductible', 'tobacco', 'dental waiting period'.")
    dataset: Dataset | None = Field(default=None, description="Limit the search to one dataset.")
    plan_years: list[PlanYear] | None = Field(default=None, max_length=MAX_PLAN_YEARS,
                                              description="Flag whether each variable exists in these years.")
    max_results: int = Field(default=20, ge=1, le=50)


class PrepareDataRequestInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request: DataRequest


class PullPlanDataInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request: DataRequest
    confirmed: bool = Field(description="Set true ONLY after the user explicitly approved the restated request.")
    request_fingerprint: str = Field(pattern=FINGERPRINT_PATTERN,
                                     description="The request_fingerprint returned by prepare_data_request.")
    limit: int = Field(default=DEFAULT_ROW_LIMIT, ge=1, le=MAX_ROW_LIMIT)

    @model_validator(mode="after")
    def require_confirmed_unchanged_request(self) -> "PullPlanDataInput":
        if not self.confirmed:
            raise ValueError("Not confirmed. Call prepare_data_request, show the user the summary, "
                             "and set confirmed=true only after they approve.")
        if self.request_fingerprint != self.request.fingerprint():
            raise ValueError("This request differs from the one that was prepared. "
                             "Call prepare_data_request again with the exact request.")
        return self


class VerifyDataPullInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request: DataRequest
    request_fingerprint: str = Field(pattern=FINGERPRINT_PATTERN)
    reported_matching_rows: int = Field(ge=0, description="total_matching_rows returned by pull_plan_data.")


# ---------- Outputs (the verification report is added once the SpecPack files are in) ----------

class ToolError(BaseModel):
    ok: Literal[False] = False
    error_code: str
    message: str
    suggestions: list[str] = Field(default_factory=list)


class VariableMatch(BaseModel):
    dataset: Dataset
    variable: str
    label: str | None
    description: str | None
    notes: str | None
    plan_years: list[int]
    missing_in_requested_years: list[int]
    numeric_filterable: bool
    unit: str | None


class FindVariablesResult(BaseModel):
    ok: Literal[True] = True
    topic: str
    matches: list[VariableMatch]
    next_step: str


class PreparedRequest(BaseModel):
    ok: Literal[True] = True
    status: Literal["needs_answers", "ready_for_confirmation"]
    request: DataRequest
    request_fingerprint: str
    plain_language_summary: str
    problems: list[str]
    questions_for_user: list[str]
    next_step: str


class SourceFile(BaseModel):
    plan_year: int
    source_url: str
    file_sha256: str
    cms_row_count: int


class PullResult(BaseModel):
    ok: Literal[True] = True
    request_fingerprint: str
    total_matching_rows: int
    returned_rows: int
    truncated: bool
    columns: list[str]
    rows: list[dict[str, str | float | int | None]]
    sources: list[SourceFile]
    next_step: str