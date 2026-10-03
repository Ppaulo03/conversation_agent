"""Tool = concrete operation available to the agent (DESIGN §5)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from conversation_agent.core.definitions.capability import Risk


class HTTPRequestSpec(BaseModel):
    """How an HTTP tool maps its (validated) arguments onto a request.

    The path is relative to a pre-registered *connection*; neither the LLM nor the tool
    definition supplies absolute URLs (DESIGN §30). `{name}` segments are path params.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    path: str
    query: tuple[str, ...] = ()
    body: tuple[str, ...] = ()


class RecoverySpec(BaseModel):
    """What the reconciliation worker may do about an `unknown` outcome (DESIGN §13).

    retry_same_key  re-send with the same idempotency key (needs `idempotency_supported`)
    status_lookup   ask the external system what happened, via a read capability
    human_handoff   escalate: no safe automatic recovery
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy: Literal["retry_same_key", "status_lookup", "human_handoff"]
    lookup_capability: str | None = None  # required for status_lookup


class ToolDefinition(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    name: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel] | None = None
    risk: Risk = "read"
    confirmation_required: bool = False
    timeout_seconds: float = 5.0
    provider: str  # key resolved by ToolRunner, e.g. "http", "fake"
    connection: str | None = None
    http: HTTPRequestSpec | None = None
    idempotency_supported: bool = False
    recovery: RecoverySpec | None = None  # non-read tools default to human_handoff

    @model_validator(mode="after")
    def _http_requires_connection_and_spec(self) -> ToolDefinition:
        if self.provider == "http" and (self.http is None or self.connection is None):
            raise ValueError(f"http tool {self.name!r} requires `http` spec and `connection`")
        return self

    @model_validator(mode="after")
    def _recovery_is_consistent_with_idempotency(self) -> ToolDefinition:
        r = self.recovery
        if r is None:
            return self
        if r.strategy == "retry_same_key" and not self.idempotency_supported:
            raise ValueError(f"{self.name!r}: retry_same_key requires idempotency_supported")
        if r.strategy == "status_lookup" and not r.lookup_capability:
            raise ValueError(f"{self.name!r}: status_lookup requires lookup_capability")
        return self

    @property
    def effective_recovery(self) -> RecoverySpec:
        """Safe default: without an explicit contract, an unknown outcome goes to a human."""
        return self.recovery or RecoverySpec(strategy="human_handoff")
