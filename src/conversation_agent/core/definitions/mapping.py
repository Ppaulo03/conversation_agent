"""Restricted, verifiable mapping language (DESIGN §6.4). No arbitrary code execution.

Supported expressions:
  "$.a.b[0]"                              path select / rename
  {"from": "$.x", "transform": "name"}     unit/format conversion
  {"from": "$.x", "enum": {"a": "B"}}      enum mapping
  {"const": 1}                             constant
  {"from_each": "$.items[*]", "map": {...}} list mapping
"""

from __future__ import annotations

from typing import Any, Union

from pydantic import BaseModel, ConfigDict, Field


class _Expr(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)


class Ref(_Expr):
    from_: str = Field(alias="from")
    transform: str | None = None
    enum: dict[str, Any] | None = None
    default: Any = None  # honoured only when explicitly set (see `model_fields_set`)


class Const(_Expr):
    const: Any


class Each(_Expr):
    from_each: str
    map: dict[str, MapExpr]


MapExpr = Union[str, Ref, Const, Each]  # noqa: UP007  (recursive alias needs a forward ref)
Each.model_rebuild()

MappingSpec = dict[str, MapExpr]

# Names the runtime implements (`tools.mapping.TRANSFORMS`; a test keeps both in sync). The
# compiler rejects any other name before the agent can run.
TRANSFORM_NAMES: frozenset[str] = frozenset(
    {"minutes_to_hours", "hours_to_minutes", "datetime_to_utc"}
)
