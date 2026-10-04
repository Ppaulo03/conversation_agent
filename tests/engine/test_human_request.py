"""Phase 12: asking for a person (rules, optional) and free-text slots that route questions."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from conversation_agent.core.compiler import agent_document
from conversation_agent.core.definitions.agent import HumanRequest
from conversation_agent.core.definitions.flow import SlotDefinition
from conversation_agent.engine.flow_understanding import looks_like_question, matches_request
from support.support_domain import compiled_support
from vertical_slice.wiring import load_compiled_agent

TRIGGERS = ("falar com atendente", "falar com um atendente", "atendente humano")


@pytest.mark.parametrize(
    "text",
    [
        "Quero falar com atendente",
        "QUERO FALAR COM UM ATENDENTE!!",
        "preciso de um atendente humano agora",
        "  falar   com atendente  ",
    ],
)
def test_a_short_request_for_a_person_matches(text: str) -> None:
    assert matches_request(text, TRIGGERS, max_words=12)


@pytest.mark.parametrize(
    "text",
    [
        "não quero falar com atendente",
        "nunca preciso falar com atendente",
        "sem falar com atendente por favor",
        "quero falar com o gerente",  # not a listed phrase
        "",
        "oi",
        "o atendente humano que me atendeu ontem foi ótimo e resolveu o meu problema todo",
    ],
)
def test_negations_other_requests_and_long_messages_do_not(text: str) -> None:
    assert not matches_request(text, TRIGGERS, max_words=12)


def test_the_word_limit_is_the_authors() -> None:
    long = "eu gostaria muito de poder falar com atendente por favor"
    assert not matches_request(long, TRIGGERS, max_words=6)
    assert matches_request(long, TRIGGERS, max_words=20)


@pytest.mark.parametrize(
    ("text", "question"),
    [
        ("quanto custa?", True),
        ("Como recupero minha senha", True),
        ("qual o horário", True),
        ("por que isso acontece", True),
        ("o que aconteceu", True),
        ("o sistema caiu", False),
        ("não consigo entrar no sistema", False),
        ("minha conta foi bloqueada.", False),
    ],
)
def test_a_question_is_sniffed_conservatively(text: str, question: bool) -> None:
    assert looks_like_question(text) is question


# --- definitions ---


def test_a_human_request_needs_real_triggers_and_a_reply() -> None:
    HumanRequest(triggers=("falar com atendente",), reply="Claro.")
    for bad in (
        {"triggers": (), "reply": "x"},
        {"triggers": ("atendente",), "reply": "x"},  # one word matches too much
        {"triggers": ("falar com atendente",), "reply": "  "},
        {"triggers": ("falar com atendente",), "reply": "x", "max_words": 1},
        {"triggers": ("falar com atendente",), "reply": "x", "surprise": 1},
    ):
        with pytest.raises(ValidationError):
            HumanRequest.model_validate(bad)
    assert HumanRequest(triggers=("falar com atendente",), reply="x").available is True


def test_question_check_only_applies_to_text_slots() -> None:
    SlotDefinition(name="s", type="text", prompt="?", question_check="model")
    with pytest.raises(ValidationError, match="only applies to text slots"):
        SlotDefinition(
            name="s", type="enum", prompt="?", choices={"a": ("b",)}, question_check="model"
        )
    assert SlotDefinition(name="s", type="text", prompt="?").question_check == "off"


def test_agents_that_do_not_use_the_new_features_keep_the_digest_they_always_had() -> None:
    plain: dict[str, Any] = agent_document(load_compiled_agent().agent)
    assert "human_request" not in plain
    assert all("question_check" not in slot for f in plain["flows"] for slot in f["slots"])
    used = agent_document(compiled_support().agent)
    assert used["human_request"]["available"] is True
    assert any(slot.get("question_check") == "model" for f in used["flows"] for slot in f["slots"])


def test_the_support_agent_declares_both_features() -> None:
    agent = compiled_support().agent
    assert agent.human_request is not None and agent.human_request.available
    subject = next(s for s in agent.flows[0].slots if s.name == "subject")
    assert subject.question_check == "model"
