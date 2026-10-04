"""One record per LLM call: who asked, why, what it consumed, how long it took, how it ended.

This is the unit of cost accounting. It carries identifiers and numbers, never prompts or answers:
it is kept for as long as the books must be kept, and it is not personal data (the conversation
appears only as the non-reversible `conversation_ref`).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Used when a call is made outside any bound context: spend is never invisible, only unattributed.
UNATTRIBUTED_TENANT = "_unattributed"
GroupBy = Literal["day", "agent_id", "agent_version", "provider", "model", "purpose"]


class LLMCallRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str
    started_at: datetime
    purpose: str = "agent"
    agent_id: str | None = None
    agent_version: str | None = None
    conversation_ref: str | None = None
    turn_id: str | None = None
    trace_id: str | None = None
    request_id: str | None = None
    provider: str | None = None
    model: str | None = None
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)
    latency_ms: float = Field(default=0.0, ge=0)
    outcome: Literal["ok", "error"] = "ok"
    error_code: str | None = None  # the exception type, never its message
    stop_reason: str | None = None


class UsageQuery(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str
    since: datetime
    until: datetime
    group_by: tuple[GroupBy, ...] = ()
    agent_id: str | None = None


class UsageRow(BaseModel):
    """Aggregated usage for one group. `model`/`provider` are always present internally because
    cost depends on them; `rollup` merges them away when the caller did not group by them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    keys: dict[str, str | None]
    provider: str | None = None
    model: str | None = None
    calls: int = 0
    errors: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    latency_p50_ms: float = 0.0
    latency_p95_ms: float = 0.0
    # The PRICED part only: `unpriced_calls > 0` means the true cost is higher (a lower bound).
    cost_usd: float = 0.0
    unpriced_calls: int = 0
