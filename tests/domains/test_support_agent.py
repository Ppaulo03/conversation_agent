"""Phase 9: a second domain (FAQ + support tickets) with nothing added to the framework.

The agent is a manifest: capabilities, a Flow and bindings over an MCP server. These tests run the
real engine against the real reference MCP server; the model is scripted, and where a Flow can
answer, it must (the model is never called)."""

from __future__ import annotations

from typing import Any

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


async def test_known_limit_a_free_text_slot_takes_whatever_is_said_next(mcp: McpHandle) -> None:
    """CHARACTERIZATION, not an endorsement: while the Flow awaits a free-text slot (the ticket
    subject), a question is taken as the subject, because the rules read any text for the slot
    being asked. Structured slots (the priority) do not have this problem. Recorded in
    IMPLEMENTATION_STATUS as a finding for the framework; this phase changes nothing in it."""
    chat = Chat(mcp, FakeLLM([]))
    await chat.say("Quero abrir um chamado")
    out = await chat.say("quanto custa a consulta?")
    assert chat.state.active_flow is not None
    assert chat.state.active_flow.slots["subject"] == "quanto custa a consulta?"
    assert "prioridade" in out.reply
