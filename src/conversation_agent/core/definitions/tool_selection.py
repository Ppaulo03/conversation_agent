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


class ToolSelectionSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = False
    provider: Literal["none", "rules"] = "none"
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

    @model_validator(mode="after")
    def _coherent(self) -> ToolSelectionSettings:
        if self.enabled and self.max_tools_per_call is None and self.max_tool_tokens is None:
            raise ValueError("tool_selection needs max_tools_per_call and/or max_tool_tokens")
        if self.enabled and self.provider == "none":
            raise ValueError("tool_selection is enabled but provider is 'none'")
        if self.provider == "rules" and self.enabled and not self.rules:
            raise ValueError("provider 'rules' needs at least one rule")
        names = [r.capability for r in self.rules]
        if len(set(names)) != len(names):
            raise ValueError("duplicate capability in tool_selection.rules")
        return self
