"""Builds validated, canonical CapabilityRequests from raw (untrusted) LLM arguments."""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from conversation_agent.core.canonical import canonicalize, stable_hash
from conversation_agent.core.definitions.capability import CapabilityDefinition
from conversation_agent.core.display import display_value
from conversation_agent.core.models.tooling import CapabilityRequest, ToolError


class RequestRejected(Exception):
    """Raised when raw arguments do not satisfy the Capability input schema."""

    def __init__(self, error: ToolError) -> None:
        super().__init__(error.message_safe)
        self.error = error


def _rejected(message: str) -> RequestRejected:
    return RequestRejected(ToolError(code="INVALID_CAPABILITY_ARGUMENTS", message_safe=message))


def _summary(capability: CapabilityDefinition, shown_args: dict[str, Any]) -> str:
    """Rendered from the validated args as the user gave them (original offsets), not from the
    canonical UTC form."""
    if capability.summary_template:
        try:
            shown = {name: display_value(value, None) for name, value in shown_args.items()}
            return capability.summary_template.format_map(shown)
        except (KeyError, IndexError, ValueError):
            pass
    pairs = ", ".join(f"{k}={v}" for k, v in sorted(shown_args.items()))
    return f"{capability.name}({pairs})"


def build_capability_request(
    capability: CapabilityDefinition, raw_args: dict[str, Any]
) -> CapabilityRequest:
    """Validate against the Capability schema (never the API's), then canonicalise + hash.

    Unknown fields are rejected, not ignored: the LLM must not be able to smuggle trusted
    context (`tenant_id`, `contact_id`, ...) through arguments (INV-002).
    """
    unknown = sorted(set(raw_args) - set(capability.input_model.model_fields))
    if unknown:
        raise _rejected(
            f"Invalid arguments for {capability.name}: unknown fields {', '.join(unknown)}."
        )
    try:
        parsed = capability.input_model.model_validate(raw_args)
    except ValidationError as exc:
        fields = sorted({".".join(str(p) for p in e["loc"]) or "<root>" for e in exc.errors()})
        raise _rejected(f"Invalid arguments for {capability.name}: {', '.join(fields)}.") from exc
    canonical = canonicalize(parsed.model_dump(mode="python"))
    assert isinstance(canonical, dict)
    return CapabilityRequest(
        capability=capability.name,
        args=canonical,
        args_hash=stable_hash(capability.name, canonical),
        summary=_summary(capability, parsed.model_dump(mode="json")),
    )
