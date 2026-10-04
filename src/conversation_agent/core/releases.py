"""Release management rules (DESIGN §35): which published version a conversation runs on.

Publishing makes a version AVAILABLE; a release decides which one new conversations get:

    stable      the version everyone gets by default
    candidate   an optional version offered to `candidate_percent` of conversations (canary)
    withdrawn   versions that were pulled: nobody new, and no idle conversation, stays on them

The rules are pure and deterministic so a rollout is reproducible and testable:

  - the bucket of a conversation is a stable hash, so the same conversation always falls on the
    same side of a canary and widening the percentage only ADDS conversations to it;
  - a conversation with anything in progress (a flow, a pending confirmation, a resumed turn)
    keeps the version it started on, whatever the release says (INV-028): nobody is migrated
    under a pending action. Only an idle conversation moves, and only when its own version is
    withdrawn or a newer version is meant for it. Versions never move backwards otherwise.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field

from conversation_agent.core.canonical import stable_hash
from conversation_agent.core.errors import ConversationAgentError
from conversation_agent.core.versioning import Version


class ReleaseError(ConversationAgentError):
    """A release operation that is not allowed (unknown version, missing evidence, ...)."""


@dataclass(frozen=True)
class ReleaseState:
    stable: str
    candidate: str | None = None
    candidate_percent: int = 0
    withdrawn: frozenset[str] = field(default_factory=frozenset)


class ReleaseEvidence(BaseModel):
    """What justified a release: the result of an eval suite against THIS agent version."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    agent_digest: str = Field(min_length=64, max_length=64)
    suite: str
    suite_digest: str = Field(min_length=64, max_length=64)
    passed: int = Field(ge=0)
    total: int = Field(ge=0)


def bucket(tenant_id: str, agent_id: str, conversation_id: str) -> int:
    """0..99, stable per conversation."""
    return int(stable_hash(tenant_id, agent_id, conversation_id)[:8], 16) % 100


def desired_version(state: ReleaseState, conv_bucket: int) -> str:
    if state.candidate is not None and conv_bucket < state.candidate_percent:
        return state.candidate
    return state.stable


def target_version(state: ReleaseState, pinned: str | None, idle: bool, conv_bucket: int) -> str:
    desired = desired_version(state, conv_bucket)
    if pinned is None:
        return desired
    if not idle:
        return pinned  # never migrate a conversation that has work in progress
    if pinned in state.withdrawn:
        return desired
    return desired if Version(desired) > Version(pinned) else pinned


def validate_evidence(
    evidence: ReleaseEvidence | None, *, agent_digest: str, required: bool
) -> None:
    if evidence is None:
        if required:
            raise ReleaseError("this release needs eval evidence for the exact version")
        return
    if evidence.agent_digest != agent_digest:
        raise ReleaseError("the eval evidence is for a different agent than the one released")
    if evidence.total == 0 or evidence.passed != evidence.total:
        raise ReleaseError(
            f"the eval evidence did not pass ({evidence.passed}/{evidence.total} scenarios)"
        )


def pick_previous(promoted: list[str], current: str, withdrawn: Collection[str]) -> str | None:
    """The most recent earlier stable that was not withdrawn (`promoted` is oldest first)."""
    for version in reversed(promoted):
        if version != current and version not in withdrawn:
            return version
    return None
