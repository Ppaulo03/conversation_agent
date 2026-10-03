"""TurnEngine: a small, controlled agent loop (DESIGN §25) driven by the Turn Journal.

Phase 1 scope: in-memory state, no Inbox/Outbox/lease/epochs, no Flow. The engine is fully
domain-independent: everything domain-specific lives in the AgentDefinition.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, Literal

from conversation_agent.core.canonical import stable_hash
from conversation_agent.core.definitions.agent import AgentDefinition
from conversation_agent.core.models.conversation import (
    ConversationIdentity,
    ConversationMessage,
    ConversationState,
    TurnOutcome,
)
from conversation_agent.core.models.journal import JournalStepType
from conversation_agent.core.models.llm import (
    LLMMessage,
    LLMRequest,
    LLMResponse,
    ToolResultPart,
    llm_request_hash,
)
from conversation_agent.core.models.tooling import CapabilityRequest, ToolContext
from conversation_agent.engine.capability_pipeline import (
    CapabilityOutcome,
    CapabilityPipeline,
    Evaluation,
)
from conversation_agent.engine.journal_steps import TurnJournalCursor
from conversation_agent.engine.prompts import build_system_prompt, render_proposal, render_result
from conversation_agent.ports.clock import Clock
from conversation_agent.ports.journal import TurnJournal
from conversation_agent.ports.llm import LLMProvider

MAX_STEPS_DEFAULT = 8


class TurnEngine:
    def __init__(
        self,
        agent: AgentDefinition,
        llm: LLMProvider,
        pipeline: CapabilityPipeline,
        journal: TurnJournal,
        clock: Clock,
        *,
        max_steps: int = MAX_STEPS_DEFAULT,
    ) -> None:
        self._agent = agent
        self._llm = llm
        self._pipeline = pipeline
        self._journal = journal
        self._clock = clock
        self._max_steps = max_steps

    async def process_turn(
        self,
        identity: ConversationIdentity,
        state: ConversationState,
        user_text: str,
        turn_id: str,
    ) -> TurnOutcome:
        """Process one turn. Re-running the same `turn_id` replays journaled steps (INV-014)."""
        cursor = TurnJournalCursor(self._journal, turn_id)

        async def aggregate() -> dict[str, Any]:
            return {"user_text": user_text, "turn_reference_time": self._clock.now().isoformat()}

        inbound = await cursor.step(
            JournalStepType.INBOUND_AGGREGATED, stable_hash(user_text), aggregate
        )
        reference_time = datetime.fromisoformat(inbound["turn_reference_time"])  # INV-018
        system = build_system_prompt(self._agent.persona, reference_time, self._agent.timezone)

        working: list[LLMMessage] = [
            *self._history_messages(state),
            LLMMessage.text("user", user_text),
        ]
        proposals: dict[str, CapabilityRequest] = {}
        llm_calls = 0
        reply: str | None = None

        for _ in range(self._max_steps):
            response = await self._llm_step(cursor, turn_id, system, working)
            llm_calls += 1
            calls = response.tool_calls
            if not calls:
                reply = response.text.strip()
                break
            working.append(LLMMessage(role="assistant", parts=response.parts))
            result_parts: list[ToolResultPart] = []
            for call in calls:
                part = await self._capability_step(
                    cursor, identity, turn_id, call.id, call.name, call.arguments, proposals
                )
                result_parts.append(part)
            working.append(LLMMessage(role="user", parts=tuple(result_parts)))

        halted: Literal["step_limit"] | None = "step_limit" if reply is None else None
        final_reply = reply or self._agent.fallback_reply
        await cursor.step(
            JournalStepType.TURN_COMPLETED,
            None,
            _const({"reply": final_reply, "halted": halted}),
        )

        new_state = ConversationState(
            history=(
                *state.history,
                ConversationMessage(role="user", text=user_text),
                ConversationMessage(role="assistant", text=final_reply),
            ),
            proposals={**state.proposals, **proposals},
        )
        return TurnOutcome(
            turn_id=turn_id,
            reply=final_reply,
            state=new_state,
            llm_calls=llm_calls,
            halted=halted,
        )

    def _history_messages(self, state: ConversationState) -> list[LLMMessage]:
        window = state.history[-self._agent.max_history_messages :]
        while window and window[0].role != "user":  # providers require a leading user message
            window = window[1:]
        return [LLMMessage.text(m.role, m.text) for m in window]

    async def _llm_step(
        self, cursor: TurnJournalCursor, turn_id: str, system: str, messages: list[LLMMessage]
    ) -> LLMResponse:
        request = LLMRequest(
            system=system, messages=tuple(messages), tools=self._pipeline.exposed_tools()
        )
        request_hash = llm_request_hash(request)
        llm_request_id = stable_hash(turn_id, cursor.next_index, request_hash)[:32]
        # LLM_REQUEST is persisted *before* the external call (DESIGN §25.1).
        await cursor.step(
            JournalStepType.LLM_REQUEST, request_hash, _const({"llm_request_id": llm_request_id})
        )

        async def call_llm() -> dict[str, Any]:
            response = await self._llm.complete(
                request.model_copy(update={"request_id": llm_request_id})
            )
            return response.model_dump(mode="json")

        payload = await cursor.step(JournalStepType.LLM_RESPONSE, request_hash, call_llm)
        return LLMResponse.model_validate(payload)

    async def _capability_step(
        self,
        cursor: TurnJournalCursor,
        identity: ConversationIdentity,
        turn_id: str,
        tool_call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        proposals: dict[str, CapabilityRequest],
    ) -> ToolResultPart:
        capability = self._pipeline.capability_name_for(tool_name)
        # tool_call_id is ephemeral and deliberately excluded from every identity (§9.1).
        request_hash = stable_hash(capability, arguments)
        logical_step_id = f"{turn_id}:{cursor.next_index}"

        await cursor.step(
            JournalStepType.CAPABILITY_REQUEST,
            request_hash,
            _const({"capability": capability, "arguments": arguments}),
            logical_step_id=logical_step_id,
        )

        async def decide() -> dict[str, Any]:
            return self._pipeline.evaluate(capability, arguments).model_dump(mode="json")

        evaluation = Evaluation.model_validate(
            await cursor.step(JournalStepType.POLICY_DECISION, request_hash, decide)
        )

        async def execute() -> dict[str, Any]:
            context = self._tool_context(identity, turn_id, logical_step_id, evaluation)
            return (await self._pipeline.execute(evaluation, context)).model_dump(mode="json")

        outcome = CapabilityOutcome.model_validate(
            await cursor.step(
                JournalStepType.TOOL_RESULT, request_hash, execute, logical_step_id=logical_step_id
            )
        )
        if outcome.proposal is not None:
            proposals[outcome.proposal.capability] = outcome.proposal
            return ToolResultPart(
                tool_call_id=tool_call_id, content=render_proposal(outcome.proposal)
            )
        assert outcome.result is not None
        return ToolResultPart(
            tool_call_id=tool_call_id,
            content=render_result(outcome.result),
            is_error=outcome.result.status != "success",
        )

    def _tool_context(
        self,
        identity: ConversationIdentity,
        turn_id: str,
        logical_step_id: str,
        evaluation: Evaluation,
    ) -> ToolContext:
        """Trusted context: built only from runtime-known values (INV-002)."""
        args_hash = evaluation.request.args_hash if evaluation.request else ""
        return ToolContext(
            tenant_id=identity.tenant_id,
            agent_id=self._agent.agent_id,
            agent_version=self._agent.version,
            channel_id=identity.channel_id,
            conversation_id=identity.conversation_id,
            session_id=identity.session_id,
            contact_id=identity.contact_id,
            turn_id=turn_id,
            invocation_id=stable_hash(
                identity.tenant_id, identity.conversation_id, turn_id, logical_step_id, args_hash
            )[:32],
            trace_id=f"trace-{turn_id}",
        )


def _const(payload: dict[str, Any]) -> Callable[[], Awaitable[dict[str, Any]]]:
    async def produce() -> dict[str, Any]:
        return payload

    return produce
