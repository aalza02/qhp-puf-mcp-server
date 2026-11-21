"""SQL safety rules: the only way column names and user values enter a query.

- Values NEVER go into SQL text. Each one becomes a named parameter (:p0, :p1, ...) that the
  SQL warehouse binds separately, which prevents SQL injection.
- Column names cannot be parameters in SQL, so each must (1) match ^[a-z][a-z0-9_]*$ and
  (2) appear in variable_catalog for the dataset before it is quoted into the query.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

SAFE_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")


class UnsafeIdentifierError(ValueError):
    """Raised when a column name is malformed or not on the allow-list."""


def quote_column(name: str, allowed_columns: set[str]) -> str:
    """Returns `name` wrapped in backticks, but only if it passes both safety checks."""
    if not SAFE_IDENTIFIER.fullmatch(name or ""):
        raise UnsafeIdentifierError(f"'{name}' is not a valid variable name.")
    if name not in allowed_columns:
        raise UnsafeIdentifierError(f"'{name}' is not a known variable for this dataset.")
    return f"`{name}`"


@dataclass
class QueryParameters:
    """Collects values for one query and hands out their :pN placeholders."""

    items: list[tuple[str, str | None, str]] = field(default_factory=list)

    def add(self, value: object, sql_type: str) -> str:
        name = f"p{len(self.items)}"
        self.items.append((name, None if value is None else str(value), sql_type))
        return f":{name}"