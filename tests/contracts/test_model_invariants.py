"""Phase 1.1 hardening: models enforce their own invariants at the boundary."""

from __future__ import annotations

from itertools import combinations

import pytest
from pydantic import BaseModel, ValidationError

from conversation_agent.core.definitions.agent import AgentDefinition
from conversation_agent.core.definitions.capability import CapabilityDefinition
from conversation_agent.core.errors import DefinitionError
from conversation_agent.core.models.tooling import (
    CapabilityRequest,
    CapabilityResult,
    PolicyDecision,
    ToolError,
    ToolResult,
)
from conversation_agent.engine.capability_pipeline import CapabilityOutcome, Evaluation

ERR = ToolError(code="X", message_safe="x")
REQ = CapabilityRequest(capability="a.b", args={}, args_hash="h")
ALLOW = PolicyDecision(outcome="allow", reason="r")
DENY = PolicyDecision(outcome="deny", reason="r")


class Empty(BaseModel):
    pass


def cap(name: str) -> CapabilityDefinition:
    return CapabilityDefinition(name=name, description="d", input_model=Empty, output_model=Empty)


# --- capability names / LLM tool names ---------------------------------------------------


@pytest.mark.parametrize(
    "name", ["a.b__c", "a__b.c", "a.b_", "_a.b", "A.b", "nodots", "a.", ".a", "a..b"]
)
def test_ambiguous_or_malformed_capability_names_are_rejected(name: str) -> None:
    with pytest.raises(ValidationError):
        cap(name)


def test_the_historical_collision_is_now_impossible() -> None:
    """`a.b__c` and `a__b.c` both used to become `a__b__c` and silently shadow each other."""
    for name in ("a.b__c", "a__b.c"):
        with pytest.raises(ValidationError):
            cap(name)


def test_llm_names_are_injective_over_valid_names() -> None:
    names = ["a.b", "a.b_c", "a_b.c", "a.b.c", "ab.c", "a.bc", "scheduling.create", "x1.y_2"]
    llm_names = [cap(n).llm_name for n in names]
    assert len(set(llm_names)) == len(names)
    assert all("." not in n for n in llm_names)
    for a, b in combinations(names, 2):
        assert cap(a).llm_name != cap(b).llm_name


def test_agent_rejects_duplicate_capabilities() -> None:
    with pytest.raises(DefinitionError):
        AgentDefinition(
            agent_id="a",
            version="1",
            persona="p",
            capabilities=(cap("a.b"), cap("a.b")),
            tools=(),
            bindings=(),
            allowed_capabilities=frozenset(),
        )


# --- ToolResult / CapabilityResult -------------------------------------------------------


@pytest.mark.parametrize("model", [ToolResult, CapabilityResult])
class TestStatusPayloadInvariant:
    def test_success_cannot_carry_an_error(self, model: type) -> None:
        with pytest.raises(ValidationError):
            model(status="success", error=ERR)

    @pytest.mark.parametrize(
        "status",
        [
            "validation_error",
            "business_error",
            "policy_denied",
            "technical_error",
            "timeout",
            "unknown",
        ],
    )
    def test_failures_require_an_error_and_forbid_data(self, model: type, status: str) -> None:
        with pytest.raises(ValidationError):
            model(status=status)
        with pytest.raises(ValidationError):
            model(status=status, data={"booking_id": "b1"}, error=ERR)
        assert model(status=status, error=ERR).status == status

    def test_success_with_or_without_data_is_valid(self, model: type) -> None:
        assert model(status="success").error is None
        assert model(status="success", data={"k": 1}).data == {"k": 1}


# --- CapabilityOutcome / Evaluation ------------------------------------------------------


def test_outcome_requires_exactly_one_of_result_or_proposal() -> None:
    ok = CapabilityResult(status="success")
    with pytest.raises(ValidationError):
        CapabilityOutcome()
    with pytest.raises(ValidationError):
        CapabilityOutcome(result=ok, proposal=REQ)
    assert CapabilityOutcome(result=ok).proposal is None
    assert CapabilityOutcome(proposal=REQ).result is None


def test_evaluation_shape_follows_the_decision() -> None:
    assert Evaluation(capability="a.b", decision=DENY).request is None
    assert Evaluation(capability="a.b", decision=ALLOW, request=REQ).rejection is None
    assert Evaluation(capability="a.b", decision=ALLOW, rejection=ERR).request is None
    with pytest.raises(ValidationError):  # allowed but neither
        Evaluation(capability="a.b", decision=ALLOW)
    with pytest.raises(ValidationError):  # both
        Evaluation(capability="a.b", decision=ALLOW, request=REQ, rejection=ERR)
    with pytest.raises(ValidationError):  # denied but carrying a request
        Evaluation(capability="a.b", decision=DENY, request=REQ)


def test_invalid_state_cannot_survive_a_journal_round_trip() -> None:
    """Journal payloads are revalidated on replay: a corrupt row fails at the boundary."""
    with pytest.raises(ValidationError):
        CapabilityOutcome.model_validate({"result": None, "proposal": None})
    with pytest.raises(ValidationError):
        ToolResult.model_validate({"status": "unknown", "data": {"booking_id": "b"}})
