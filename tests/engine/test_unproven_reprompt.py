"""The unproven-yes wording is optional and never changes the digest of agents that do not set it."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from conversation_agent.core.compiler import agent_document, compile_manifest
from conversation_agent.core.definitions.agent import ConfirmationTexts
from support.support_domain import manifest


def test_an_agent_that_does_not_set_it_keeps_the_digest_it_always_had() -> None:
    raw = manifest()
    with_it = compile_manifest(raw)
    assert with_it.agent.confirmation.reprompt_unproven is not None
    assert "reprompt_unproven" in agent_document(with_it.agent)["confirmation"]

    raw["confirmation"].pop("reprompt_unproven")
    without = compile_manifest(raw)
    assert "reprompt_unproven" not in agent_document(without.agent)["confirmation"]
    assert without.digest != with_it.digest


def test_the_wording_must_carry_the_summary() -> None:
    raw = manifest()
    raw["confirmation"]["reprompt_unproven"] = "Não consegui confirmar."
    with pytest.raises(Exception, match="summary"):
        compile_manifest(raw)
    assert ConfirmationTexts().reprompt_unproven is None
    with pytest.raises(ValidationError):
        ConfirmationTexts.model_validate({"reprompt_unproven": 3, "surprise": 1})
