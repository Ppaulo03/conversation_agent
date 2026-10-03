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

    @model_validator(mode="after")
    def _http_requires_connection_and_spec(self) -> ToolDefinition:
        if self.provider == "http" and (self.http is None or self.connection is None):
            raise ValueError(f"http tool {self.name!r} requires `http` spec and `connection`")
        return self
