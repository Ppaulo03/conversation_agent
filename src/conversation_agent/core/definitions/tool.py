"""Tool = concrete operation available to the agent (DESIGN §5)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from conversation_agent.core.definitions.capability import Risk
from conversation_agent.core.definitions.mapping import MappingSpec


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
    # Optional tool arguments that are deliberately NOT sent (runtime-only). Anything else the
    # binding can produce must travel in the path, query or body, or it would silently vanish.
    ignored: tuple[str, ...] = ()


class MCPToolSpec(BaseModel):
    """How a tool maps onto a tool published by an MCP server (DESIGN §32).

    `remote_name` is the server's name for it. `schema_digest` PINS the server's input schema at
    the moment the operator allowed the tool: a server that later changes what the tool accepts
    is not the tool that was reviewed, so nothing is sent until someone looks again (INV-037).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    remote_name: str
    schema_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class RecoverySpec(BaseModel):
    """What the reconciliation worker may do about an `unknown` outcome (DESIGN §13).

    safe_retry      re-run a READ (no side effect, repeating is harmless)
    retry_same_key  re-send with the same idempotency key (needs `idempotency_supported`)
    status_lookup   ask the external system what happened, via a read capability;
                    only a `business_error` whose code is in `absent_codes` proves "it did
                    not happen" and allows a same-key re-send (any other error is not proof)
    human_handoff   escalate: no safe automatic recovery
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy: Literal["safe_retry", "retry_same_key", "status_lookup", "human_handoff"]
    lookup_capability: str | None = None  # required for status_lookup
    # How the lookup capability's output becomes the ORIGINAL capability's output. Without it the
    # two output schemas must be identical (checked when the agent is built).
    result_map: MappingSpec | None = None
    absent_codes: tuple[str, ...] = ()  # lookup error codes that prove the operation is absent


class RetryPolicy(BaseModel):
    """Technical retry of ONE operation (never a new semantic attempt).

    `max_attempts > 1` on a write/irreversible tool is only valid with idempotency support and
    `same_idempotency_key` (INV-021): a retry must be indistinguishable from the first request
    to the external system. Ambiguous outcomes (`unknown`) are never retried here (INV-006).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_attempts: int = 1
    mode: Literal["none", "same_idempotency_key"] = "none"
    delay_seconds: float = 0.0


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
    mcp: MCPToolSpec | None = None
    idempotency_supported: bool = False
    retry: RetryPolicy = RetryPolicy()
    recovery: RecoverySpec | None = None  # non-read tools default to human_handoff

    @model_validator(mode="after")
    def _mcp_requires_connection_and_spec(self) -> ToolDefinition:
        if self.provider == "mcp" and (self.mcp is None or self.connection is None):
            raise ValueError(f"mcp tool {self.name!r} requires `mcp` spec and `connection`")
        if self.mcp is not None and self.provider != "mcp":
            raise ValueError(f"tool {self.name!r} has an `mcp` spec but provider {self.provider!r}")
        return self

    @model_validator(mode="after")
    def _http_requires_connection_and_spec(self) -> ToolDefinition:
        if self.provider == "http" and (self.http is None or self.connection is None):
            raise ValueError(f"http tool {self.name!r} requires `http` spec and `connection`")
        return self

    @model_validator(mode="after")
    def _retry_rules(self) -> ToolDefinition:
        """The "compiler" rules for retries, enforced when the definition is built."""
        r = self.retry
        if r.max_attempts < 1:
            raise ValueError(f"{self.name!r}: retry.max_attempts must be >= 1")
        if r.max_attempts > 1 and self.risk != "read":
            if not self.idempotency_supported:
                raise ValueError(
                    f"{self.name!r}: a {self.risk} tool cannot retry without idempotency support"
                )
            if r.mode != "same_idempotency_key":
                raise ValueError(
                    f"{self.name!r}: a {self.risk} tool may only retry with the SAME "
                    "idempotency key"
                )
        return self

    @model_validator(mode="after")
    def _recovery_is_consistent_with_idempotency(self) -> ToolDefinition:
        r = self.recovery
        if r is None:
            return self
        if r.strategy == "retry_same_key" and not self.idempotency_supported:
            raise ValueError(f"{self.name!r}: retry_same_key requires idempotency_supported")
        if r.strategy == "safe_retry" and self.risk != "read":
            raise ValueError(f"{self.name!r}: safe_retry is only valid for read tools")
        if r.strategy == "status_lookup" and not r.lookup_capability:
            raise ValueError(f"{self.name!r}: status_lookup requires lookup_capability")
        return self

    @property
    def effective_recovery(self) -> RecoverySpec:
        """Default when no contract is declared: reads are safely re-run, anything that can
        change the world goes to a human."""
        if self.recovery is not None:
            return self.recovery
        return RecoverySpec(strategy="safe_retry" if self.risk == "read" else "human_handoff")
