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
from conversation_agent.core.definitions.capability import RISK_ORDER
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
    for name, cap in sorted(old_caps.items()):
        current = new_caps.get(name)
        if current is None:
            found.append(f"capability {name!r} was removed")
            continue
        if RISK_ORDER[current["risk"]] < RISK_ORDER[cap["risk"]]:
            found.append(f"capability {name!r} lowered its risk {cap['risk']} -> {current['risk']}")
        if current["input"] != cap["input"]:
            found.append(f"capability {name!r} changed its input schema")
        if current["output"] != cap["output"]:
            found.append(f"capability {name!r} changed its output schema")
    old_flows = {f["name"] for f in before["flows"]}
    for name in sorted(old_flows - {f["name"] for f in after["flows"]}):
        found.append(f"flow {name!r} was removed")
    return tuple(found)


def plan_publish(existing: list[CompiledAgent], new: CompiledAgent) -> PublishPlan:
    same = next((e for e in existing if e.version == new.version), None)
    if same is not None:
        if same.digest == new.digest:
            return PublishPlan("idempotent")
        raise VersionConflictError(
            f"{new.agent_id} {new.version} is already published with different content; "
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
