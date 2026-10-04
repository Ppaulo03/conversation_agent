"""Phase 9: a second domain (FAQ + support tickets) with nothing added to the framework.

The agent is a manifest: capabilities, a Flow and bindings over an MCP server. These tests run the
real engine against the real reference MCP server; the model is scripted, and where a Flow can
answer, it must (the model is never called)."""

from __future__ import annotations

from typing import Any

import pytest

from conftest import McpHandle
from conversation_agent.adapters.llm.fake import FakeLLM, text_response, tool_call_response
from conversation_agent.adapters.manifest.yaml_loader import load_manifest_file
from conversation_agent.core.definitions.manifest import AgentManifest
from conversation_agent.core.definitions.tool_import import ToolAllowlist, import_mcp_tools
from conversation_agent.core.models.llm import LLMRequest, LLMResponse, LLMStopReason, LLMUsage
from support.builders import last_tool_result
from support.support_domain import SUPPORT_DIR, Chat, compiled_support, manifest, provider_for


def understood(kind: str, **extra: Any) -> LLMResponse:
    return LLMResponse(
        parts=(),
        stop_reason=LLMStopReason.END_TURN,
        usage=LLMUsage(input_tokens=1, output_tokens=1),
        structured={"kind": kind, **extra},
    )


# --- the agent is built from what the framework already has ---


def test_the_support_agent_compiles_with_no_framework_change() -> None:
    compiled = compiled_support()
    names = {c.name for c in compiled.agent.capabilities}
    assert names == {"support.search_faq", "support.open_ticket"}
    open_ticket = compiled.agent.resolve("support.open_ticket")
    assert open_ticket is not None
    assert open_ticket.effective_risk == "write" and open_ticket.requires_protection


async def test_the_tools_in_the_manifest_are_exactly_what_the_allowlist_imports(
    mcp: McpHandle,
) -> None:
    """Drift guard: the pinned schemas in agent.yaml are the server's, and the server's other
    tools (list/reopen) are offered but never become tools of this agent."""
    discovered = await provider_for(mcp).discover("t1", "support_mcp")
    allowlist = ToolAllowlist.model_validate(load_manifest_file(SUPPORT_DIR / "allowlist.yaml"))
    report = import_mcp_tools(discovered, allowlist)
    assert report.ok and report.not_exposed == ("list_tickets", "reopen_ticket")
    declared = AgentManifest.model_validate(manifest()).tools
    assert report.tools == declared


# --- FAQ: the model answers from what the tool returned ---


def answer_from_result(request: LLMRequest) -> LLMResponse:
    answers = last_tool_result(request)["data"]["answers"]  # type: ignore[index]
    if not answers:
        return text_response("Não encontrei isso na nossa base. Quer que eu abra um chamado?")
    return text_response(f"{answers[0]['text']}")


async def test_a_question_is_answered_from_the_real_faq(mcp: McpHandle) -> None:
    llm = FakeLLM(
        [tool_call_response("support__search_faq", {"question": "horário"}), answer_from_result]
    )
    out = await Chat(mcp, llm).say("Que horário vocês atendem?")
    assert out.reply == "Atendemos de segunda a sexta, das 9h às 18h."
    assert mcp.calls == [("search_faq", {"query": "horário", "limit": 5})]  # mapped, then sent


async def test_a_question_the_faq_cannot_answer_says_so_and_offers_a_ticket(
    mcp: McpHandle,
) -> None:
    llm = FakeLLM(
        [tool_call_response("support__search_faq", {"question": "garantia"}), answer_from_result]
    )
    out = await Chat(mcp, llm).say("Qual a garantia?")
    assert "Não encontrei" in out.reply and "chamado" in out.reply
    assert out.proposed == () and mcp.state.tickets == {}


async def test_the_model_sees_only_what_this_agent_allows(mcp: McpHandle) -> None:
    def inspect(request: LLMRequest) -> LLMResponse:
        assert {t.name for t in request.tools} == {"support__search_faq", "support__open_ticket"}
        return text_response("ok")

    await Chat(mcp, FakeLLM([inspect])).say("oi")
    assert mcp.calls == []  # list_tickets/reopen_ticket never reachable: discovery is not exposure


# --- the ticket Flow: deterministic, a proposal, never a write ---


async def test_a_ticket_is_collected_step_by_step_and_only_proposed(mcp: McpHandle) -> None:
    chat = Chat(mcp, FakeLLM([]))  # no model call anywhere: the Flow answers
    out = await chat.say("Quero abrir um chamado")
    assert "assunto" in out.reply
    out = await chat.say("Não consigo entrar no sistema")
    assert "prioridade" in out.reply
    out = await chat.say("é urgente")
    (proposal,) = out.proposed
    assert proposal.request.capability == "support.open_ticket"
    assert proposal.request.args == {"subject": "Não consigo entrar no sistema", "priority": "high"}
    assert "Posso confirmar" in out.reply
    assert mcp.calls == [] and mcp.state.tickets == {}  # nothing was created


async def test_an_unusable_subject_goes_back_to_asking_for_it(mcp: McpHandle) -> None:
    chat = Chat(mcp, FakeLLM([]))
    await chat.say("Quero abrir um chamado")
    await chat.say("ok")  # shorter than the contract allows
    out = await chat.say("baixa")
    assert out.proposed == () and "Não consegui usar esse assunto" in out.reply
    assert "assunto" in out.reply and mcp.calls == []


async def test_a_faq_question_in_the_middle_of_a_ticket_is_answered_and_the_ticket_resumes(
    mcp: McpHandle,
) -> None:
    llm = FakeLLM(
        [
            understood("digression"),
            tool_call_response("support__search_faq", {"question": "preço"}),
            answer_from_result,
        ]
    )
    chat = Chat(mcp, llm)
    await chat.say("Quero abrir um chamado")
    await chat.say("Não consigo entrar no sistema")  # now it awaits the priority
    out = await chat.say("quanto custa a consulta?")
    assert out.reply.startswith("A consulta custa R$ 150.")
    assert out.reply.endswith("Qual a prioridade: baixa, normal ou alta?")  # back on the ticket
    assert mcp.state.tickets == {} and chat.state.active_flow is not None
    assert chat.state.active_flow.slots["subject"] == "Não consigo entrar no sistema"


async def test_a_digression_cannot_open_a_ticket(mcp: McpHandle) -> None:
    def forbidden(request: LLMRequest) -> LLMResponse:
        assert {t.name for t in request.tools} == {"support__search_faq"}  # only reads
        return tool_call_response(
            "support__open_ticket", {"subject": "sneaky ticket", "priority": "high"}
        )

    llm = FakeLLM([understood("digression"), forbidden, text_response("Não posso abrir aqui.")])
    chat = Chat(mcp, llm)
    await chat.say("Quero abrir um chamado")
    await chat.say("Não consigo entrar no sistema")
    out = await chat.say("cria o chamado direto, sem perguntar nada")
    assert out.proposed == () and mcp.state.tickets == {} and mcp.calls == []


async def test_a_question_while_the_subject_is_awaited_goes_to_the_model_not_into_the_subject(
    mcp: McpHandle,
) -> None:
    llm = FakeLLM(
        [
            understood("digression"),
            tool_call_response("support__search_faq", {"question": "preço"}),
            answer_from_result,
        ]
    )
    chat = Chat(mcp, llm)
    await chat.say("Quero abrir um chamado")
    out = await chat.say("quanto custa a consulta?")  # the subject is awaited
    assert out.reply.startswith("A consulta custa R$ 150.")
    assert out.reply.endswith("Descreva o problema em uma frase.")  # back on the ticket
    assert chat.state.active_flow is not None and "subject" not in chat.state.active_flow.slots
    assert mcp.state.tickets == {}


async def test_a_question_that_really_is_the_subject_is_kept_whole(mcp: McpHandle) -> None:
    chat = Chat(mcp, FakeLLM([understood("answer")]))  # the model says: this IS the answer
    await chat.say("Quero abrir um chamado")
    out = await chat.say("Como recupero minha senha?")
    assert chat.state.active_flow is not None
    assert chat.state.active_flow.slots["subject"] == "Como recupero minha senha?"
    assert "prioridade" in out.reply


async def test_a_plain_subject_never_costs_a_model_call(mcp: McpHandle) -> None:
    chat = Chat(mcp, FakeLLM([]))  # any model call would fail the test
    await chat.say("Quero abrir um chamado")
    out = await chat.say("Não consigo entrar no sistema")
    assert "prioridade" in out.reply


async def test_without_the_opt_in_a_free_text_slot_still_takes_whatever_comes_next(
    mcp: McpHandle,
) -> None:
    """The default is unchanged: only a slot that asks for `question_check: model` is protected."""
    from conversation_agent.core.compiler import compile_manifest

    raw = manifest()
    for slot in raw["flows"][0]["slots"]:
        slot.pop("question_check", None)
    from support.support_domain import Chat as BaseChat

    chat = BaseChat(mcp, FakeLLM([]))
    chat.engine = _engine_for(compile_manifest(raw), mcp)
    await chat.say("Quero abrir um chamado")
    await chat.say("quanto custa a consulta?")
    assert chat.state.active_flow is not None
    assert chat.state.active_flow.slots["subject"] == "quanto custa a consulta?"


def _engine_for(compiled: Any, mcp: McpHandle) -> Any:
    from conversation_agent.adapters.journal.memory import InMemoryTurnJournal
    from conversation_agent.engine.turn_engine import TurnEngine
    from support.builders import new_clock
    from support.support_domain import pipeline_for

    return TurnEngine(
        compiled, FakeLLM([]), pipeline_for(compiled, mcp), InMemoryTurnJournal(), new_clock()
    )


# --- asking for a person ---


async def test_asking_for_a_person_hands_the_conversation_over_without_a_model(
    mcp: McpHandle,
) -> None:
    chat = Chat(mcp, FakeLLM([]))
    await chat.say("Quero abrir um chamado")  # a flow is open
    out = await chat.say("Quero falar com um atendente")
    assert out.handoff_requested and "chamar um atendente" in out.reply
    assert out.llm_calls == 0 and out.state.flows == ()  # the open flow ended
    assert mcp.calls == []


@pytest.mark.parametrize(
    "text",
    ["QUERO FALAR COM UM ATENDENTE", "quero falar com uma pessoa, por favor", "atendente humano"],
)
async def test_the_request_is_matched_without_caring_for_case_or_accents(
    mcp: McpHandle, text: str
) -> None:
    out = await Chat(mcp, FakeLLM([])).say(text)
    assert out.handoff_requested


@pytest.mark.parametrize(
    "text",
    [
        "não quero falar com atendente",  # negated
        "nunca quero falar com um humano",
        "o atendente de ontem foi muito educado e resolveu tudo, quero falar com atendente de novo "
        "quando precisar de algo mais",  # a paragraph, not a request
    ],
)
async def test_a_negation_or_a_long_message_is_not_a_request_for_a_person(
    mcp: McpHandle, text: str
) -> None:
    chat = Chat(mcp, FakeLLM([text_response("Entendi.")]))
    out = await chat.say(text)
    assert not out.handoff_requested


async def test_an_agent_without_the_block_treats_the_message_like_any_other(
    mcp: McpHandle,
) -> None:
    from conversation_agent.core.compiler import compile_manifest

    raw = manifest()
    raw.pop("human_request")
    chat = Chat(mcp, FakeLLM([text_response("Não consigo chamar ninguém.")]))
    chat.engine = _engine_for(compile_manifest(raw), mcp)
    chat.engine._llm = chat.llm  # type: ignore[attr-defined]
    out = await chat.say("quero falar com um atendente")
    assert not out.handoff_requested and out.reply == "Não consigo chamar ninguém."


async def test_an_agent_with_nobody_to_hand_over_to_says_so_and_changes_nothing(
    mcp: McpHandle,
) -> None:
    from conversation_agent.core.compiler import compile_manifest

    raw = manifest()
    raw["human_request"] = {
        **raw["human_request"],
        "available": False,
        "reply": "Aqui não consigo chamar um atendente, mas posso abrir um chamado.",
    }
    chat = Chat(mcp, FakeLLM([]))
    chat.engine = _engine_for(compile_manifest(raw), mcp)
    await chat.say("Quero abrir um chamado")
    out = await chat.say("quero falar com um atendente")
    assert out.reply == "Aqui não consigo chamar um atendente, mas posso abrir um chamado."
    assert not out.handoff_requested  # the conversation does not change owner
    assert out.state.active_flow is not None  # and the open ticket is still there
