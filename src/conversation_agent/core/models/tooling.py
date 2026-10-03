"""Canonical tool/capability execution models (DESIGN §8, §23.4, §39A)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ToolStatus = Literal[
    "success",
    "validation_error",
    "business_error",
    "policy_denied",
    "technical_error",
    "timeout",
    "unknown",
]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ToolContext(_Frozen):
    """Trusted context. Injected by the runtime; never produced by the LLM (INV-002)."""

    tenant_id: str
    agent_id: str
    agent_version: str
    channel_id: str
    conversation_id: str
    session_id: str
    contact_id: str
    turn_id: str
    invocation_id: str
    trace_id: str


class ToolError(_Frozen):
    """Safe error description. Never carries secrets or the raw external body."""

    code: str
    message_safe: str
    retryable: bool = False
    details_ref: str | None = None


def _check_status_payload(status: ToolStatus, data: object, error: object) -> None:
    """success <=> data without error; every other status carries an error and no data."""
    if status == "success":
        if error is not None:
            raise ValueError("a successful result cannot carry an error")
    else:
        if error is None:
            raise ValueError(f"status {status!r} requires an error")
        if data is not None:
            raise ValueError(f"status {status!r} cannot carry data")


class ToolResult(_Frozen):
    status: ToolStatus
    data: dict[str, Any] | list[Any] | None = None
    error: ToolError | None = None
    provider_metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _consistent(self) -> ToolResult:
        _check_status_payload(self.status, self.data, self.error)
        return self


class CapabilityRequest(_Frozen):
    """A validated, canonical request for a Capability.

    `args` is the canonical form (datetimes in UTC, sorted keys) on which `args_hash`
    is computed; it contains only business arguments, never trusted context.
    """

    capability: str
    args: dict[str, Any]
    args_hash: str
    summary: str | None = None  # for confirmation prompts; NOT part of args_hash


class CapabilityResult(_Frozen):
    """Outcome of a capability call as seen by the engine/LLM (Capability schema)."""

    status: ToolStatus
    data: dict[str, Any] | None = None
    error: ToolError | None = None
    # Operational facts from the provider (http status, external request ids, ...). Recorded in
    # the ledger for recovery; deliberately never rendered to the LLM.
    provider_metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _consistent(self) -> CapabilityResult:
        _check_status_payload(self.status, self.data, self.error)
        return self


class PolicyDecision(_Frozen):
    outcome: Literal["allow", "deny", "require_confirmation"]
    reason: str
