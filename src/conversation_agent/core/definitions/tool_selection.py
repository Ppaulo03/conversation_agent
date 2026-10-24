"""Per-agent settings of tool selection (ADR-0XX): shrink the tool list the model sees.

Selection only ever REMOVES tools from the list the capability, policy and allowlist layers already
allowed; it never adds one. It is off by default and, when on, only acts once a configured limit is
exceeded (tool count, estimated schema tokens, or either).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ToolRule(BaseModel):
    """`rules` provider: a capability is relevant when the message contains any keyword (accent
    and case insensitive; matches from the start of a word, so plurals are found).
    Score = 1 - 0.5**matched keywords."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    capability: str
    keywords: tuple[str, ...] = Field(min_length=1)


class CircuitSettings(BaseModel):
    """Circuit breaker of the `http` provider, per (tenant, connection), in memory: after
    `failure_threshold` consecutive health failures (unreachable, timeout, 5xx/429, unusable
    answer) the server is not called for `open_seconds` and the model gets the full list."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = True
    failure_threshold: int = Field(default=5, ge=1)
    open_seconds: float = Field(default=30.0, gt=0.0)
    half_open_probes: int = Field(default=1, ge=1)


class HttpSelectorSettings(BaseModel):
    """`http` provider: an external ranking server, reached through a pre-registered per-tenant
    CONNECTION (URL, host allowlist, TLS policy and the credential's secret reference all belong to
    the operator, as for tools); the manifest only names the connection. Wire contract: ADR-029
    and `docs/TOOL_SELECTOR_CONTRACT.md`. The user's message text is sent to that server."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    connection: str
    path: str = "/tool-selection"
    timeout_seconds: float = Field(default=2.0, gt=0.0, le=30.0)  # capped by the connection
    circuit: CircuitSettings = CircuitSettings()

    @model_validator(mode="after")
    def _path_is_only_a_path(self) -> HttpSelectorSettings:
        if not self.path.startswith("/") or any(c in self.path for c in "?#") or ".." in self.path:
            raise ValueError("http.path must be an absolute path without query, fragment or '..'")
        return self


class JevSelectorSettings(HttpSelectorSettings):
    """`jev` provider: a hosted decision model (TypeSafe Jev) asked ONE `choice` question over the
    candidate tools; the probabilities become scores. Same connection model as `http`: URL, host
    allowlist and the API key's secret reference belong to the operator. The user's message text
    and the tool names/descriptions are sent to that service. Wire format: ADR-031."""

    path: str = "/v1/systemone"
    timeout_seconds: float = Field(default=5.0, gt=0.0, le=30.0)
    model: str = Field(default="jev-latest", min_length=1)
    instructions: str = Field(
        default="Escolha a ferramenta mais adequada para atender a mensagem do usuário.",
        min_length=1,
        max_length=500,
    )


class ToolSelectionSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = False
    provider: Literal["none", "rules", "http", "jev"] = "none"
    # Triggers. Selection runs when ANY configured limit is exceeded; with none configured, never.
    max_tools_per_call: int | None = Field(default=None, ge=1)
    max_tool_tokens: int | None = Field(default=None, ge=1)  # estimated, see `estimate_tokens`
    top_n: int = Field(default=8, ge=1)
    min_score: float = Field(default=0.15, ge=0.0, le=1.0)
    always_include: tuple[str, ...] = ()  # capability names kept whatever the provider says
    on_error: Literal["full_list"] = "full_list"  # provider failure = the unfiltered list
    # The model asked for a tool the filter removed: repeat that call ONCE with the full list.
    retry_with_full_list: bool = True
    # Above this share of fallbacks (over the recent window) the filter switches itself off.
    max_fallback_rate: float | None = Field(default=None, gt=0.0, le=1.0)
    rules: tuple[ToolRule, ...] = ()
    http: HttpSelectorSettings | None = None
    jev: JevSelectorSettings | None = None

    @model_validator(mode="after")
    def _coherent(self) -> ToolSelectionSettings:
        if self.enabled and self.max_tools_per_call is None and self.max_tool_tokens is None:
            raise ValueError("tool_selection needs max_tools_per_call and/or max_tool_tokens")
        if self.enabled and self.provider == "none":
            raise ValueError("tool_selection is enabled but provider is 'none'")
        if self.provider == "rules" and self.enabled and not self.rules:
            raise ValueError("provider 'rules' needs at least one rule")
        if self.provider == "http" and self.http is None:
            raise ValueError("provider 'http' needs an http block (connection, path, timeout)")
        if self.provider != "http" and self.http is not None:
            raise ValueError("an http block needs provider 'http'")
        if self.provider == "jev" and self.jev is None:
            raise ValueError("provider 'jev' needs a jev block (connection, model)")
        if self.provider != "jev" and self.jev is not None:
            raise ValueError("a jev block needs provider 'jev'")
        names = [r.capability for r in self.rules]
        if len(set(names)) != len(names):
            raise ValueError("duplicate capability in tool_selection.rules")
        return self
