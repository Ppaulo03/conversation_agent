"""Capability = stable semantic contract consumed by Flow/LLM (DESIGN §4)."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

Risk = Literal["read", "write", "irreversible"]
RISK_ORDER: dict[Risk, int] = {"read": 0, "write": 1, "irreversible": 2}

# `domain.action[.sub]`: lower-case segments joined by single dots. Segments cannot contain
# "__", which makes `name.replace(".", "__")` (the LLM-facing name) injective: two distinct
# capability names can never collide on the same tool name.
_CAPABILITY_NAME = re.compile(
    r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*(?:\.[a-z][a-z0-9]*(?:_[a-z0-9]+)*)+$"
)


class CapabilityDefinition(BaseModel):
    """Input/output are typed Pydantic models: the Flow/LLM only ever sees these schemas,
    never the schema of the concrete external API."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    name: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    risk: Risk = "read"
    confirmation_required: bool = False

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        if not _CAPABILITY_NAME.fullmatch(value):
            raise ValueError(
                f"invalid capability name {value!r}: expected dotted lower_snake segments "
                "such as 'domain.action' (no '__', no leading/trailing '_')"
            )
        return value

    @property
    def llm_name(self) -> str:
        """Name exposed to the LLM (providers reject dots). Injective by construction."""
        return self.name.replace(".", "__")
