"""Runtime settings for the QHP PUF MCP server.

Everything workspace-specific (catalog, schema, SQL warehouse) comes from environment
variables, so the public repository never contains workspace names, URLs, or secrets.
Authentication is handled by the Databricks SDK using credentials that Databricks Apps
provides automatically; no token is ever stored in code.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

UNITY_CATALOG_NAME = re.compile(r"^[A-Za-z0-9_]+$")
REQUIRED_VARIABLES = ("QHP_CATALOG", "QHP_SCHEMA", "DATABRICKS_WAREHOUSE_ID")


@dataclass(frozen=True)
class Settings:
    catalog: str
    schema: str
    warehouse_id: str
    query_timeout_seconds: int = 50  # the SQL Statement API allows 5-50 seconds of waiting

    def table(self, table_name: str) -> str:
        """Fully qualified, backtick-quoted table name, safe to place in SQL text."""
        if not UNITY_CATALOG_NAME.match(table_name):
            raise ValueError(f"Unsafe table name: {table_name!r}")
        return f"`{self.catalog}`.`{self.schema}`.`{table_name}`"


def load_settings() -> Settings:
    values = {name: os.environ.get(name, "").strip() for name in REQUIRED_VARIABLES}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise RuntimeError(f"Missing required environment variable(s): {', '.join(missing)}")
    for name in ("QHP_CATALOG", "QHP_SCHEMA"):
        if not UNITY_CATALOG_NAME.match(values[name]):
            raise RuntimeError(f"{name} may contain only letters, numbers, and underscores.")
    timeout = int(os.environ.get("QHP_QUERY_TIMEOUT_SECONDS", "50"))
    return Settings(
        catalog=values["QHP_CATALOG"],
        schema=values["QHP_SCHEMA"],
        warehouse_id=values["DATABRICKS_WAREHOUSE_ID"],
        query_timeout_seconds=max(5, min(timeout, 50)),
    )