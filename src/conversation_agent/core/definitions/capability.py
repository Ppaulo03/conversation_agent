"""Capability = stable semantic contract consumed by Flow/LLM (DESIGN §4)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

Risk = Literal["read", "write", "irreversible"]
RISK_ORDER: dict[Risk, int] = {"read": 0, "write": 1, "irreversible": 2}


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
