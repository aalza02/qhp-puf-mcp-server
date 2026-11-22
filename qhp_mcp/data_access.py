"""Read-only data access for the QHP PUF tables.

Every statement here is a SELECT. Every user value is a named parameter, and every column
name passes sql_safety.quote_column first. Errors raised to callers carry a short, safe
message; technical details go only to the server log.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import (
    Disposition,
    ExecuteStatementRequestOnWaitTimeout,
    Format,
    StatementParameterListItem,
    StatementState,
)

from .config import Settings
from .models import MAX_ROW_LIMIT, DataFilter, DataRequest
from .sql_safety import QueryParameters, quote_column

log = logging.getLogger(__name__)

CATALOG_CACHE_SECONDS = 600  # the catalog changes only when the pipeline is re-run
INTEGER_TYPES = {"INT", "LONG", "SHORT", "BYTE"}
DECIMAL_TYPES = {"DECIMAL", "DOUBLE", "FLOAT"}
COMPARISON_SQL = {"greater_than": ">", "greater_or_equal": ">=", "less_than": "<", "less_or_equal": "<="}


class DataAccessError(RuntimeError):
    """A failure whose message is safe to show the chatbot user."""


@dataclass(frozen=True)
class QueryResult:
    columns: list[str]
    rows: list[dict]


def _convert(value: str | None, type_name: str) -> object:
    """The SQL API returns every value as text; restore numbers and booleans."""
    if value is None:
        return None
    if type_name in INTEGER_TYPES:
        return int(value)
    if type_name in DECIMAL_TYPES:
        return float(value)
    if type_name == "BOOLEAN":
        return value.lower() == "true"
    return value


class QhpDataAccess:
    def __init__(self, settings: Settings, client: WorkspaceClient | None = None):
        self._settings = settings
        self._client = client or WorkspaceClient()  # credentials come from the environment
        self._catalog: list[dict] | None = None
        self._catalog_loaded_at = 0.0

    # ---------- low-level execution ----------

    def _run(self, sql: str, params: QueryParameters | None = None) -> QueryResult:
        parameters = [StatementParameterListItem(name=n, value=v, type=t) for n, v, t in (params.items if params else [])]
        try:
            response = self._client.statement_execution.execute_statement(
                warehouse_id=self._settings.warehouse_id,
                statement=sql,
                parameters=parameters,
                wait_timeout=f"{self._settings.query_timeout_seconds}s",
                on_wait_timeout=ExecuteStatementRequestOnWaitTimeout.CANCEL,
                disposition=Disposition.INLINE,
                format=Format.JSON_ARRAY,
            )
        except Exception:
            log.exception("SQL request could not be sent")
            raise DataAccessError("The data service could not be reached. Please try again in a minute.") from None

        state = response.status.state if response.status else None
        if state != StatementState.SUCCEEDED:
            detail = response.status.error.message if response.status and response.status.error else "no detail"
            log.error("Statement %s ended as %s: %s", response.statement_id, state, detail)
            if state == StatementState.CANCELED:
                raise DataAccessError("The query took too long and was stopped. Try fewer plan years or add filters.")
            raise DataAccessError("The query could not be completed. Please check the request and try again.")

        columns = [c.name for c in response.manifest.schema.columns]
        types = [c.type_name.value if c.type_name else "STRING" for c in response.manifest.schema.columns]
        data = list(response.result.data_array or []) if response.result else []
        next_chunk = response.result.next_chunk_index if response.result else None
        while next_chunk is not None:  # large results arrive in pieces
            chunk = self._client.statement_execution.get_statement_result_chunk_n(response.statement_id, next_chunk)
            data += chunk.data_array or []
            next_chunk = chunk.next_chunk_index
        rows = [{c: _convert(v, t) for c, v, t in zip(columns, row, types)} for row in data]
        return QueryResult(columns=columns, rows=rows)

    # ---------- catalog and provenance ----------

    def variable_catalog(self) -> list[dict]:
        """All catalog rows (344 today). Cached briefly because every tool needs it."""
        if self._catalog is None or time.monotonic() - self._catalog_loaded_at > CATALOG_CACHE_SECONDS:
            sql = (f"SELECT dataset, variable, plan_year, description, variable_label, notes, cms_variable_name, "
                   f"is_numeric, numeric_column, unit, non_null_rows, description_source "
                   f"FROM {self._settings.table('variable_catalog')} ORDER BY dataset, variable, plan_year")
            self._catalog = self._run(sql).rows
            self._catalog_loaded_at = time.monotonic()
        return self._catalog

    def _column_rules(self, dataset: str) -> tuple[set[str], dict[str, str]]:
        """Allow-list of column names for a dataset, plus variable -> numeric twin column."""
        entries = [e for e in self.variable_catalog() if e["dataset"] == dataset]
        numeric = {e["variable"]: e["numeric_column"] for e in entries if e["is_numeric"] and e["numeric_column"]}
        allowed = {e["variable"] for e in entries} | set(numeric.values()) | {"plan_year"}
        return allowed, numeric

    def source_manifest(self, dataset: str, plan_years: list[int]) -> list[dict]:
        params = QueryParameters()
        dataset_marker = params.add(dataset, "STRING")
        year_markers = ", ".join(params.add(y, "INT") for y in plan_years)
        sql = (f"SELECT dataset, plan_year, source_url, file_sha256, row_count "
               f"FROM {self._settings.table('source_manifest')} "
               f"WHERE dataset = {dataset_marker} AND plan_year IN ({year_markers}) ORDER BY plan_year")
        return self._run(sql, params).rows

    def loaded_row_counts(self, dataset: str, plan_years: list[int]) -> dict[int, int]:
        params = QueryParameters()
        year_markers = ", ".join(params.add(y, "INT") for y in plan_years)
        sql = (f"SELECT plan_year, COUNT(*) AS loaded_rows FROM {self._settings.table(dataset)} "
               f"WHERE plan_year IN ({year_markers}) GROUP BY plan_year")
        return {r["plan_year"]: r["loaded_rows"] for r in self._run(sql, params).rows}

    # ---------- request-driven queries ----------

    def _where(self, request: DataRequest, params: QueryParameters, skip_filter: int | None = None) -> str:
        allowed, numeric = self._column_rules(request.dataset)
        year_markers = ", ".join(params.add(y, "INT") for y in request.plan_years)
        clauses = [f"`plan_year` IN ({year_markers})"]
        for index, data_filter in enumerate(request.filters):
            if index != skip_filter:
                clauses.append(self._filter_clause(data_filter, allowed, numeric, params))
        return " AND ".join(clauses)

    @staticmethod
    def _filter_clause(f: DataFilter, allowed: set[str], numeric: dict[str, str], params: QueryParameters) -> str:
        if f.is_numeric:
            if f.variable not in numeric:
                raise DataAccessError(f"'{f.variable}' can't be filtered as a number. Use equals, not_equals, or in.")
            column = quote_column(numeric[f.variable], allowed)
            if f.operator == "between":
                return f"{column} BETWEEN {params.add(f.value[0], 'DOUBLE')} AND {params.add(f.value[1], 'DOUBLE')}"
            return f"{column} {COMPARISON_SQL[f.operator]} {params.add(f.value, 'DOUBLE')}"
        column = quote_column(f.variable, allowed)
        # Text matching ignores capital letters and stray spaces (CMS writes '$375 ' with a space).
        if f.operator == "in":
            markers = ", ".join(params.add(v.lower(), "STRING") for v in f.value)
            return f"LOWER(TRIM({column})) IN ({markers})"
        sign = "=" if f.operator == "equals" else "<>"
        return f"LOWER(TRIM({column})) {sign} {params.add(f.value.lower(), 'STRING')}"

    def count_matching_rows(self, request: DataRequest) -> dict[int, int]:
        params = QueryParameters()
        where = self._where(request, params)
        sql = (f"SELECT plan_year, COUNT(*) AS matching_rows FROM {self._settings.table(request.dataset)} "
               f"WHERE {where} GROUP BY plan_year")
        counts = {r["plan_year"]: r["matching_rows"] for r in self._run(sql, params).rows}
        return {year: counts.get(year, 0) for year in request.plan_years}

    def fetch_rows(self, request: DataRequest, limit: int) -> QueryResult:
        allowed, _ = self._column_rules(request.dataset)
        columns = ["plan_year"] + [v for v in request.variables if v != "plan_year"]
        select_list = ", ".join(quote_column(c, allowed) for c in columns)
        params = QueryParameters()
        where = self._where(request, params)
        safe_limit = max(1, min(int(limit), MAX_ROW_LIMIT))
        # Sorting by every returned column makes repeated pulls return the same rows.
        sql = (f"SELECT {select_list} FROM {self._settings.table(request.dataset)} "
               f"WHERE {where} ORDER BY {select_list} LIMIT {safe_limit}")
        return self._run(sql, params)

    def null_counts(self, request: DataRequest) -> list[dict]:
        allowed, _ = self._column_rules(request.dataset)
        null_sums = ", ".join(
            f"SUM(CASE WHEN {quote_column(v, allowed)} IS NULL THEN 1 ELSE 0 END) AS `nulls__{v}`"
            for v in request.variables)
        params = QueryParameters()
        where = self._where(request, params)
        sql = (f"SELECT plan_year, COUNT(*) AS matching_rows, {null_sums} "
               f"FROM {self._settings.table(request.dataset)} WHERE {where} GROUP BY plan_year ORDER BY plan_year")
        results = []
        for row in self._run(sql, params).rows:
            for v in request.variables:
                results.append({"variable": v, "plan_year": row["plan_year"],
                                "null_rows": row[f"nulls__{v}"], "matching_rows": row["matching_rows"]})
        return results

    def non_numeric_excluded(self, request: DataRequest) -> list[dict]:
        """For each numeric filter: rows that met every other condition but were skipped because
        CMS wrote text (e.g. 'Not Applicable') instead of a number. Reported, never hidden."""
        allowed, numeric = self._column_rules(request.dataset)
        results = []
        for index, f in enumerate(request.filters):
            if not f.is_numeric or f.variable not in numeric:
                continue
            params = QueryParameters()
            where = self._where(request, params, skip_filter=index)
            text_col, num_col = quote_column(f.variable, allowed), quote_column(numeric[f.variable], allowed)
            sql = (f"SELECT plan_year, COUNT(*) AS excluded_rows FROM {self._settings.table(request.dataset)} "
                   f"WHERE {where} AND {text_col} IS NOT NULL AND {num_col} IS NULL GROUP BY plan_year")
            counts = {r["plan_year"]: r["excluded_rows"] for r in self._run(sql, params).rows}
            results += [{"variable": f.variable, "plan_year": y, "excluded_rows": counts.get(y, 0)}
                        for y in request.plan_years]
        return results
    
    def distinct_values(self, dataset: str, variable: str, plan_years: list[int], max_values: int = 25) -> list[str] | None:
        """The variable's distinct values (trimmed), or None if it has more than max_values (IDs, free text)."""
        allowed, _ = self._column_rules(dataset)
        column = quote_column(variable, allowed)
        params = QueryParameters()
        year_markers = ", ".join(params.add(y, "INT") for y in plan_years)
        sql = (f"SELECT DISTINCT TRIM({column}) AS value FROM {self._settings.table(dataset)} "
               f"WHERE plan_year IN ({year_markers}) AND {column} IS NOT NULL LIMIT {max_values + 1}")
        values = sorted(r["value"] for r in self._run(sql, params).rows)
        return None if len(values) > max_values else values