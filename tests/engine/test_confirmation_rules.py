"""Deterministic confirmation logic: interpretation and prompt eligibility (INV-022)."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from conversation_agent.core.models.actions import (
    PendingAction,
    PromptEligibility,
    PromptRecord,
)
from conversation_agent.core.models.runtime import InboundRef, OutboxStatus
from conversation_agent.core.models.tooling import CapabilityRequest
from conversation_agent.engine.confirmation import assess_eligibility, interpret_by_rules, normalize

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=ZoneInfo("UTC"))
TOL = timedelta(seconds=2)


@pytest.mark.parametrize(
    "text",
    ["Sim", "sim!", "  SIM  ", "Confirmo", "pode ser", "Isso mesmo!", "ok", "👍", "Pode sim."],
)
def test_plain_affirmations_confirm(text: str) -> None:
    assert interpret_by_rules(text) == "confirm"


@pytest.mark.parametrize("text", ["Não", "nao", "NÃO!", "cancela", "melhor não", "desisto"])
def test_plain_refusals_reject(text: str) -> None:
    assert interpret_by_rules(text) == "reject"


@pytest.mark.parametrize(
    "text",
    [
        "Sim, mas às 16h",
        "sim mas na quinta",
        "ok, só que às 11",
        "Pode ser, mas prefiro outro horário",
        "sim, às 16h",
        "não, quero quinta",
        "Não, prefiro às 15h",
        "nao, outro dia",
    ],
)
def test_a_confirmation_that_carries_a_change_is_modify_never_confirm(text: str) -> None:
    """'sim, mas às 16h' must never authorise the ORIGINAL action."""
    assert interpret_by_rules(text) == "modify"


@pytest.mark.parametrize(
    "text",
    ["quanto custa?", "qual o endereço?", "talvez", "hmm", "", "   ", "!!!", "sim por gentileza"],
)
def test_anything_else_is_left_to_the_fallback(text: str) -> None:
    assert interpret_by_rules(text) is None


def test_normalize_strips_accents_case_and_punctuation() -> None:
    assert normalize("  Sim, mas às 16h!! ") == "sim mas as 16h"


# --- eligibility ---------------------------------------------------------------------------------


def action(latest: str | None = "p2") -> PendingAction:
    return PendingAction(
        tenant_id="t",
        conversation_id="c",
        action_id="a1",
        capability="x.y",
        tool_name="tool",
        request=CapabilityRequest(capability="x.y", args={}, args_hash="h"),
        args_hash="h",
        protected_fields=(),
        summary="s",
        created_from_turn="turn",
        latest_prompt_outbox_id=latest,
    )


def prompt(
    outbox_id: str = "p2",
    status: OutboxStatus = OutboxStatus.ACCEPTED,
    provider_id: str | None = "pm-2",
    accepted: datetime | None = T0,
) -> PromptRecord:
    return PromptRecord(
        outbox_id=outbox_id,
        status=status,
        provider_message_id=provider_id,
        provider_accepted_at=accepted,
    )


def ref(at: datetime | None, reply_to: str | None = None, event_id: str = "e1") -> InboundRef:
    return InboundRef(
        event_id=event_id, provider_occurred_at=at, reply_to_provider_message_id=reply_to
    )


def verdict(prompts: list[PromptRecord], inbound: list[InboundRef], a: PendingAction | None = None):  # type: ignore[no-untyped-def]
    return assess_eligibility(
        action=a or action(), prompts=prompts, inbound=inbound, tolerance=TOL
    ).status


def test_a_reply_clearly_after_the_accepted_prompt_is_eligible() -> None:
    assert verdict([prompt()], [ref(T0 + timedelta(seconds=30))]) is PromptEligibility.ELIGIBLE


def test_a_yes_that_predates_the_prompt_is_not_a_reply_to_it() -> None:
    assert verdict([prompt()], [ref(T0 - timedelta(seconds=30))]) is PromptEligibility.BEFORE_PROMPT


def test_the_skew_window_is_ambiguous_not_eligible() -> None:
    for offset in (-2, -1, 0, 1, 2):
        assert (
            verdict([prompt()], [ref(T0 + timedelta(seconds=offset))])
            is PromptEligibility.AMBIGUOUS
        )
    assert verdict([prompt()], [ref(T0 + timedelta(seconds=2.5))]) is PromptEligibility.ELIGIBLE
    assert (
        verdict([prompt()], [ref(T0 - timedelta(seconds=2.5))]) is PromptEligibility.BEFORE_PROMPT
    )


def test_without_comparable_clocks_it_is_ambiguous_a_local_received_at_proves_nothing() -> None:
    assert verdict([prompt()], [ref(None)]) is PromptEligibility.AMBIGUOUS
    assert verdict([prompt(accepted=None)], [ref(T0)]) is PromptEligibility.AMBIGUOUS


@pytest.mark.parametrize(
    "status",
    [OutboxStatus.PENDING, OutboxStatus.SENDING, OutboxStatus.QUEUED, OutboxStatus.UNKNOWN,
     OutboxStatus.FAILED, OutboxStatus.SUPERSEDED],
)  # fmt: skip
def test_only_an_ACCEPTED_prompt_is_eligible(status: OutboxStatus) -> None:  # INV-022
    result = verdict([prompt(status=status)], [ref(T0 + timedelta(minutes=5))])
    assert result is PromptEligibility.PROMPT_NOT_ACCEPTED


def test_an_explicit_reply_to_the_prompt_resolves_the_ambiguous_zone() -> None:
    inside_skew = T0 + timedelta(seconds=1)
    assert verdict([prompt()], [ref(inside_skew, reply_to="pm-2")]) is PromptEligibility.ELIGIBLE
    assert verdict([prompt()], [ref(None, reply_to="pm-2")]) is PromptEligibility.ELIGIBLE


def test_a_reply_to_an_older_accepted_prompt_of_the_same_action_is_valid() -> None:
    old = prompt("p1", provider_id="pm-1", accepted=T0 - timedelta(minutes=5))
    new = prompt("p2", provider_id="pm-2", status=OutboxStatus.UNKNOWN, accepted=None)
    assert verdict([old, new], [ref(None, reply_to="pm-1")]) is PromptEligibility.ELIGIBLE


def test_a_reply_to_something_else_is_unrelated() -> None:
    assert verdict([prompt()], [ref(T0 + timedelta(minutes=1), reply_to="other")]) is (
        PromptEligibility.UNRELATED_REPLY
    )


def test_a_reply_to_a_prompt_that_is_not_accepted_is_not_eligible() -> None:
    queued = prompt(status=OutboxStatus.QUEUED, provider_id="pm-2")
    assert verdict([queued], [ref(None, reply_to="pm-2")]) is PromptEligibility.PROMPT_NOT_ACCEPTED


def test_no_prompt_recorded_means_not_accepted() -> None:
    assert verdict([], [ref(T0)], action(latest=None)) is PromptEligibility.PROMPT_NOT_ACCEPTED


def test_a_burst_is_eligible_only_if_every_message_is() -> None:
    late = ref(T0 + timedelta(minutes=1), event_id="e2")
    early = ref(T0 - timedelta(minutes=1), event_id="e1")
    assert verdict([prompt()], [late, late]) is PromptEligibility.ELIGIBLE
    assert verdict([prompt()], [early, late]) is PromptEligibility.BEFORE_PROMPT
    assert verdict([prompt()], []) is PromptEligibility.AMBIGUOUS
