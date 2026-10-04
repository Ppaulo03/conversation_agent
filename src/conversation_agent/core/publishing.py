"""Publishing rules for agent versions (Phase 5), independent of where they are stored.

- a published (agent, version) is IMMUTABLE: republishing the same content is a no-op,
  different content under the same version is an error;
- versions only move forward;
- a breaking change needs a MAJOR bump: the version number is a promise to the conversations
  pinned to older versions and to the operator reading the history.
"""

from __future__ import annotations

from dataclasses import dataclass

from conversation_agent.core.compiler import CompiledAgent, agent_document
from conversation_agent.core.definitions.binding import ResolvedToolBinding
from conversation_agent.core.definitions.capability import RISK_ORDER
from conversation_agent.core.definitions.schema_spec import schema_fingerprint
from conversation_agent.core.errors import (
    IncompatibleUpgradeError,
    VersionConflictError,
    VersionRegressionError,
)
from conversation_agent.core.versioning import Version


@dataclass(frozen=True)
class PublishPlan:
    action: str  # "new" | "idempotent"
    breaking: tuple[str, ...] = ()


def breaking_changes(old: CompiledAgent, new: CompiledAgent) -> tuple[str, ...]:
    """What a conversation written against `old` could trip over when moved to `new`."""
    before, after = agent_document(old.agent), agent_document(new.agent)
    old_caps = {c["name"]: c for c in before["capabilities"]}
    new_caps = {c["name"]: c for c in after["capabilities"]}
    found: list[str] = []
    for name in sorted(old_caps):
        current = new_caps.get(name)
        if current is None:
            found.append(f"capability {name!r} was removed")
            continue
        found += _protection_changes(name, old.agent.resolve(name), new.agent.resolve(name))
        old_cap, new_cap = old.agent.resolve(name), new.agent.resolve(name)
        if old_cap is not None and new_cap is not None:
            # documentation is not part of the contract: only the COMPATIBILITY fingerprint counts
            for label, before_model, after_model in (
                ("input", old_cap.capability.input_model, new_cap.capability.input_model),
                ("output", old_cap.capability.output_model, new_cap.capability.output_model),
            ):
                if schema_fingerprint(before_model, docs=False) != schema_fingerprint(
                    after_model, docs=False
                ):
                    found.append(f"capability {name!r} changed its {label} schema")
    for name in sorted(set(before["allowed_capabilities"]) - set(after["allowed_capabilities"])):
        found.append(f"capability {name!r} is no longer allowed for the agent")
    old_flows = {f["name"] for f in before["flows"]}
    for name in sorted(old_flows - {f["name"] for f in after["flows"]}):
        found.append(f"flow {name!r} was removed")
    return tuple(found)


def _protection_changes(
    name: str, old: ResolvedToolBinding | None, new: ResolvedToolBinding | None
) -> list[str]:
    """Compares the RESOLVED security envelope (max of capability/tool/binding), never the label
    on one layer: a tool that stops being irreversible lowers protection even if the
    capability still says `read`."""
    if old is None or new is None:
        return [] if old is None else [f"capability {name!r} is no longer bound to a tool"]
    found: list[str] = []
    if RISK_ORDER[new.effective_risk] < RISK_ORDER[old.effective_risk]:
        found.append(
            f"capability {name!r} lowered its effective risk "
            f"{old.effective_risk} -> {new.effective_risk}"
        )
    if old.requires_protection and not new.requires_protection:
        found.append(f"capability {name!r} no longer requires protection/confirmation")
    elif old.effective_confirmation_required and not new.effective_confirmation_required:
        found.append(f"capability {name!r} no longer requires confirmation")
    return found


def plan_publish(existing: list[CompiledAgent], new: CompiledAgent) -> PublishPlan:
    same = next((e for e in existing if e.version == new.version), None)
    if same is not None:
        if same.manifest_digest == new.manifest_digest:
            return PublishPlan("idempotent")
        what = "different metadata" if same.digest == new.digest else "different content"
        raise VersionConflictError(
            f"{new.agent_id} {new.version} is already published with {what}; "
            "published versions are immutable: publish a new version"
        )
    if not existing:
        return PublishPlan("new")
    latest = max(existing, key=lambda e: Version(e.version))
    if Version(new.version) < Version(latest.version):
        raise VersionRegressionError(
            f"{new.agent_id} {new.version} is lower than the latest published {latest.version}"
        )
    breaking = breaking_changes(latest, new)
    if breaking and Version(new.version).major == Version(latest.version).major:
        raise IncompatibleUpgradeError(
            f"{latest.version} -> {new.version} has breaking changes and needs a MAJOR bump: "
            + "; ".join(breaking)
        )
    return PublishPlan("new", breaking)
