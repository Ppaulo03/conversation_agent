"""Confirmation logic that must be deterministic: interpretation by rules and prompt eligibility.

Nothing here calls an LLM or touches storage. The LLM fallback (for messages the rules cannot
classify) lives in the confirmation stage and is still validated by the runtime.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import NamedTuple

from conversation_agent.core.models.actions import (
    ConfirmationDecision,
    PendingAction,
    PromptEligibility,
    PromptRecord,
)
from conversation_agent.core.models.runtime import InboundRef, OutboxStatus

# --- interpretation ----------------------------------------------------------------------------

_CONFIRM = frozenset(
    {
        "sim", "s", "ss", "simm", "confirmo", "confirmado", "confirma", "confirmar", "pode",
        "pode ser", "pode sim", "sim pode", "isso", "isso mesmo", "ok", "okay", "claro", "certo",
        "fechado", "combinado", "positivo", "yes", "y", "perfeito", "otimo", "beleza", "bora",
        "com certeza", "sim confirmo", "sim por favor", "pode confirmar", "sim pode confirmar",
    }
)  # fmt: skip
_REJECT = frozenset(
    {
        "nao", "n", "nao quero", "nao obrigado", "cancela", "cancelar", "desisto", "deixa pra la",
        "deixa", "negativo", "no", "nao pode", "melhor nao", "esquece", "nao confirmo",
    }
)  # fmt: skip
_CONFIRM_STARTS = frozenset({"sim", "s", "ok", "okay", "pode", "claro", "certo", "isso", "yes"})
_REJECT_STARTS = frozenset({"nao", "n", "no", "cancela", "cancelar", "desisto", "esquece"})
_MODIFICATION_MARKERS = frozenset(
    {"mas", "porem", "so", "troca", "trocar", "muda", "mudar", "outro", "outra", "outros",
     "outras", "vez", "inves", "verdade", "melhor", "prefiro", "preferia", "queria", "quero"}
)  # fmt: skip


def normalize(text: str) -> str:
    """Lower-case, strip accents and punctuation: 'Sim, mas às 16h!' -> 'sim mas as 16h'."""
    folded = unicodedata.normalize("NFKD", text.lower())
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", " ", folded)).strip()


def interpret_by_rules(text: str) -> ConfirmationDecision | None:
    """A decision only when it is unambiguous; otherwise None (the LLM/unclear path decides).

    Conservative on purpose: a plain affirmation confirms; an affirmation that carries a change
    ("sim, mas às 16h") is `modify`, never `confirm`; a refusal with extra content is `modify`.
    """
    if "👍" in text and not normalize(text):
        return "confirm"
    norm = normalize(text)
    if not norm:
        return None
    if norm in _CONFIRM:
        return "confirm"
    if norm in _REJECT:
        return "reject"
    tokens = norm.split()
    has_digit = any(ch.isdigit() for ch in norm)
    has_marker = any(t in _MODIFICATION_MARKERS for t in tokens)
    if tokens[0] in _REJECT_STARTS and (has_marker or has_digit or len(tokens) > 1):
        return "modify"  # "não, quero quinta": a refusal that proposes something else
    if tokens[0] in _CONFIRM_STARTS and (has_marker or has_digit):
        return "modify"  # "sim, mas às 16h": never a confirmation of the ORIGINAL action
    return None


# --- prompt eligibility (INV-022) ----------------------------------------------------------------


class Eligibility(NamedTuple):
    status: PromptEligibility
    prompt: PromptRecord | None = None


_PRIORITY = [
    PromptEligibility.PROMPT_NOT_ACCEPTED,
    PromptEligibility.UNRELATED_REPLY,
    PromptEligibility.BEFORE_PROMPT,
    PromptEligibility.AMBIGUOUS,
]


def assess_eligibility(
    *,
    action: PendingAction,
    prompts: Sequence[PromptRecord],
    inbound: Sequence[InboundRef],
    tolerance: timedelta,
) -> Eligibility:
    """Is this burst a reply to an *ACCEPTED* confirmation prompt of this action?

    Evidence, in order (DESIGN §10): (1) channel reply-to; (2) provider-domain timestamps with a
    skew tolerance; (3) nothing comparable -> ambiguous (local `received_at` proves nothing).
    QUEUED/SENDING/UNKNOWN prompts never authorise anything. The burst is eligible only if
    EVERY inbound message is.
    """
    if not inbound:
        return Eligibility(PromptEligibility.AMBIGUOUS)
    by_provider_id = {
        p.provider_message_id: p for p in prompts if p.provider_message_id is not None
    }
    latest = next((p for p in prompts if p.outbox_id == action.latest_prompt_outbox_id), None)

    chosen: PromptRecord | None = None
    verdicts: list[PromptEligibility] = []
    for ref in inbound:
        verdict, prompt = _assess_one(ref, by_provider_id, latest, tolerance)
        verdicts.append(verdict)
        chosen = chosen or prompt
    if all(v is PromptEligibility.ELIGIBLE for v in verdicts):
        return Eligibility(PromptEligibility.ELIGIBLE, chosen)
    for level in _PRIORITY:
        if level in verdicts:
            return Eligibility(level, latest)
    return Eligibility(PromptEligibility.AMBIGUOUS, latest)  # pragma: no cover


def _assess_one(
    ref: InboundRef,
    by_provider_id: Mapping[str, PromptRecord],
    latest: PromptRecord | None,
    tolerance: timedelta,
) -> tuple[PromptEligibility, PromptRecord | None]:
    if ref.reply_to_provider_message_id is not None:
        replied = by_provider_id.get(ref.reply_to_provider_message_id)
        if replied is None:
            return PromptEligibility.UNRELATED_REPLY, None
        if replied.status is not OutboxStatus.ACCEPTED:
            return PromptEligibility.PROMPT_NOT_ACCEPTED, replied
        return PromptEligibility.ELIGIBLE, replied  # explicit reply-to resolves any clock doubt

    if latest is None or latest.status is not OutboxStatus.ACCEPTED:
        return PromptEligibility.PROMPT_NOT_ACCEPTED, latest
    if ref.provider_occurred_at is None or latest.provider_accepted_at is None:
        return PromptEligibility.AMBIGUOUS, latest
    delta = ref.provider_occurred_at - latest.provider_accepted_at
    if delta < -tolerance:
        return PromptEligibility.BEFORE_PROMPT, latest
    if abs(delta) <= tolerance:
        return PromptEligibility.AMBIGUOUS, latest
    return PromptEligibility.ELIGIBLE, latest
