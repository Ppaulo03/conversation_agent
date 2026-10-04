"""TurnEngine: a small, controlled agent loop (DESIGN §25) driven by the Turn Journal.

Phase 1 scope: in-memory state, no Inbox/Outbox/lease/epochs, no Flow. The engine is fully
domain-independent: everything domain-specific lives in the AgentDefinition.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, Literal

from conversation_agent.core.canonical import stable_hash
from conversation_agent.core.compiler import CompiledAgent
from conversation_agent.core.definitions.agent import AgentDefinition
from conversation_agent.core.errors import TurnCancelledError
from conversation_agent.core.models.actions import PendingAction
from conversation_agent.core.models.conversation import (
    ConversationIdentity,
    ConversationMessage,
    ConversationState,
    TurnOutcome,
)
from conversation_agent.core.models.flow import FlowInstance
from conversation_agent.core.models.journal import JournalStepType
from conversation_agent.core.models.llm import (
    LLMMessage,
    LLMRequest,
    LLMResponse,
    LLMStopReason,
    LLMStructuredOutput,
    LLMToolDefinition,
    ToolResultPart,
    llm_request_hash,
)
from conversation_agent.core.models.media import MediaReference, TranscriptionContext
from conversation_agent.core.models.runtime import InboundRef, Ownership
from conversation_agent.core.models.tooling import CapabilityRequest, ProposedAction, ToolContext
from conversation_agent.core.observability import bind
from conversation_agent.core.tracing import span, trace_id_for
from conversation_agent.engine.capability_pipeline import (
    CapabilityOutcome,
    CapabilityPipeline,
    Evaluation,
)
from conversation_agent.engine.confirmation_stage import ConfirmationStage, StageInput
from conversation_agent.engine.flow_runner import FlowRunner, FlowTurn
from conversation_agent.engine.flow_understanding import matches_request
from conversation_agent.engine.journal_steps import TurnJournalCursor
from conversation_agent.engine.media import MediaNormalizer
from conversation_agent.engine.policy_rules import PolicyContext
from conversation_agent.engine.prompts import build_system_prompt, render_proposal, render_result
from conversation_agent.engine.side_effects import DirectToolExecutor, ToolStep, ToolStepExecutor
from conversation_agent.ports.clock import Clock
from conversation_agent.ports.journal import TurnJournal
from conversation_agent.ports.llm import LLMProvider
from conversation_agent.ports.transcriber import Transcriber

MAX_STEPS_DEFAULT = 8


class TurnEngine:
    def __init__(
        self,
        agent: CompiledAgent,
        llm: LLMProvider,
        pipeline: CapabilityPipeline,
        journal: TurnJournal,
        clock: Clock,
        *,
        max_steps: int = MAX_STEPS_DEFAULT,
        tool_executor: ToolStepExecutor | None = None,
        transcriber: Transcriber | None = None,
    ) -> None:
        # The engine only runs what the compiler produced (INV-029), and every part of the graph
        # must come from THAT compiled agent (INV-030): no split-brain between definitions.
        if (pipeline.compiled.agent_id, pipeline.compiled.version, pipeline.compiled.digest) != (
            agent.agent_id,
            agent.version,
            agent.digest,
        ):
            raise ValueError("the capability pipeline was built from a different compiled agent")
        self._compiled = agent
        self._agent: AgentDefinition = agent.agent
        self._llm = llm
        self._pipeline = pipeline
        self._journal = journal
        self._clock = clock
        self._max_steps = max_steps
        self._executor: ToolStepExecutor = tool_executor or DirectToolExecutor(pipeline)
        self._confirmation: ConfirmationStage | None = None
        self._flows = FlowRunner(agent.agent) if agent.agent.flows else None
        self._media = MediaNormalizer(agent.agent, transcriber)

    @property
    def pipeline(self) -> CapabilityPipeline:
        return self._pipeline

    @property
    def agent(self) -> AgentDefinition:
        return self._agent

    @property
    def agent_ref(self) -> tuple[str, str]:
        """(agent_id, version) this engine runs: what logs and usage records are labelled with."""
        return self._compiled.agent_id, self._compiled.version

    def attach_confirmation(self, stage: ConfirmationStage) -> None:
        """Enable the protected-action confirmation stage (needs the ledger executor)."""
        if stage.compiled.digest != self._compiled.digest or (
            stage.compiled.version != self._compiled.version
        ):
            raise ValueError("the confirmation stage was built from a different compiled agent")
        self._confirmation = stage

    async def process_turn(
        self,
        identity: ConversationIdentity,
        state: ConversationState,
        user_text: str,
        turn_id: str,
        *,
        guard: Callable[[], None] | None = None,
        late_event_ids: tuple[str, ...] = (),
        pending: PendingAction | None = None,
        inbound: tuple[InboundRef, ...] = (),
        cancel: Callable[[], bool] | None = None,
        media: tuple[MediaReference, ...] = (),
    ) -> TurnOutcome:
        """Process one turn. Re-running the same `turn_id` replays journaled steps (INV-014)."""
        cursor = TurnJournalCursor(self._journal, turn_id)

        async def aggregate() -> dict[str, Any]:
            return {
                "user_text": user_text,
                "turn_reference_time": self._clock.now().isoformat(),
                "late_event_ids": list(late_event_ids),
            }

        aggregated = await cursor.step(
            JournalStepType.INBOUND_AGGREGATED, stable_hash(user_text), aggregate
        )
        reference_time = datetime.fromisoformat(aggregated["turn_reference_time"])  # INV-018
        if media:
            user_text = await self._normalize_media(cursor, identity, turn_id, user_text, media)
        system = build_system_prompt(self._agent.persona, reference_time, self._agent.timezone)

        working: list[LLMMessage] = [
            *self._history_messages(state),
            LLMMessage.text("user", user_text),
        ]
        catalog = self._pipeline.exposed_tools(  # only what this tenant may actually call
            PolicyContext(tenant_id=identity.tenant_id, ownership=Ownership.BOT, now=reference_time)
        )
        if pending is not None and self._confirmation is not None:
            staged = await self._confirmation.run(
                cursor,
                StageInput(
                    identity=identity,
                    turn_id=turn_id,
                    user_text=user_text,
                    reference_time=reference_time,
                    pending=pending,
                    inbound=inbound,
                    history=self._history_messages(state),
                    system=system,
                    tools=catalog,
                    guard=guard,
                ),
            )
            if staged.reply is not None:  # handled by the confirmation protocol
                stage_reply, stage_flows, stage_handoff = staged.reply, state.flows, False
                if self._flows is not None and staged.closed is not None:
                    followed = self._flows.after_action(
                        state.flows, staged.closed, staged.status, staged.reply
                    )
                    stage_reply, stage_flows = followed.reply, followed.flows
                    stage_handoff = followed.handoff  # "I will call a person" must really happen
                return await self._finish(
                    cursor,
                    turn_id,
                    state,
                    user_text,
                    stage_reply,
                    {},
                    (),
                    staged.llm_calls,
                    None,
                    staged.reprompt_action_id,
                    flows=stage_flows,
                    handoff=stage_handoff,
                )
            # modify / not-a-reply: fall through and treat the message as a normal turn

        try:
            return await self._converse(
                cursor,
                identity,
                state,
                user_text,
                turn_id,
                guard,
                cancel,
                system,
                catalog,
                reference_time,
                working,
            )
        except _StopAfterEffect:
            # A newer message asked to restart, but something irreversible already happened this
            # turn: report it, never drop it (the newer message is handled by the next turn).
            return await self._finish(
                cursor,
                turn_id,
                state,
                user_text,
                self._agent.cancelled_after_effect_reply,
                {},
                (),
                0,
                None,
                None,
            )

    async def _converse(
        self,
        cursor: TurnJournalCursor,
        identity: ConversationIdentity,
        state: ConversationState,
        user_text: str,
        turn_id: str,
        guard: Callable[[], None] | None,
        cancel: Callable[[], bool] | None,
        system: str,
        catalog: tuple[LLMToolDefinition, ...],
        reference_time: datetime,
        working: list[LLMMessage],
    ) -> TurnOutcome:
        proposals: dict[str, CapabilityRequest] = {}
        proposed: list[ProposedAction] = []
        policy_state = _PolicyTurnState(cancel)
        human = self._agent.human_request
        if human is not None and matches_request(user_text, human.triggers, human.max_words):
            # A person was asked for: decided by rules, before any flow or model, so it cannot
            # be argued out of. Open flows end; the conversation goes to HANDOFF_PENDING.
            if not human.available:  # nobody to hand over to: say so, change nothing
                return await self._finish(
                    cursor, turn_id, state, user_text, human.reply, {}, (), 0, None, None
                )
            return await self._finish(
                cursor, turn_id, state, user_text, human.reply, {}, (), 0, None, None,
                flows=(), handoff=True,
            )  # fmt: skip
        if self._flows is not None:
            flow_io = _EngineFlowIO(
                self,
                cursor,
                identity,
                turn_id,
                proposals,
                proposed,
                guard,
                policy_state,
                reference_time,
                system,
                self._history_messages(state),
                user_text,
                catalog,
            )
            flow_turn = await self._flows.handle(
                FlowTurn(
                    text=user_text,
                    flows=state.flows,
                    reference_time=reference_time,
                    io=flow_io,
                )
            )
            if flow_turn is not None:
                flow_reply = flow_turn.reply
                if proposed and self._agent.confirmation_prompt_enabled:
                    flow_reply = self._with_confirmation(flow_reply, proposed[-1])
                return await self._finish(
                    cursor,
                    turn_id,
                    state,
                    user_text,
                    flow_reply,
                    proposals,
                    tuple(proposed),
                    0,
                    None,
                    None,
                    flows=flow_turn.flows,
                    handoff=flow_turn.handoff,
                )

        llm_calls = 0
        reply: str | None = None
        halted: Literal["step_limit", "llm_truncated", "token_budget"] | None = None
        tokens_used = 0

        for _ in range(self._max_steps):
            self.boundary(guard, policy_state)  # safe boundary: no new step if stale/cancelled
            response = await self.llm_step(cursor, turn_id, system, working, tools=catalog)
            llm_calls += 1
            tokens_used += response.usage.input_tokens + response.usage.output_tokens
            budget = self._agent.max_tokens_per_turn
            if budget is not None and tokens_used > budget:
                halted = "token_budget"  # guardrail: no more tool calls on an exhausted budget
                break
            if response.stop_reason is LLMStopReason.MAX_TOKENS:
                # Truncated output (possibly cut-off tool arguments): never treat as a final answer.
                halted = "llm_truncated"
                break
            calls = response.tool_calls
            if not calls:
                reply = response.text.strip()
                break
            working.append(LLMMessage(role="assistant", parts=response.parts))
            result_parts: list[ToolResultPart] = []
            for call in calls:
                part = await self.capability_step(
                    cursor,
                    identity,
                    turn_id,
                    call.id,
                    call.name,
                    call.arguments,
                    proposals,
                    proposed,
                    guard,
                    policy_state,
                    reference_time,
                )
                result_parts.append(part)
            working.append(LLMMessage(role="user", parts=tuple(result_parts)))

        if reply is None and halted is None:
            halted = "step_limit"
        final_reply = reply or self._agent.fallback_reply
        if proposed and self._agent.confirmation_prompt_enabled:
            # The confirmation question is runtime-owned, not left to the model's wording.
            final_reply = self._with_confirmation(reply or "", proposed[-1])
            halted = None if reply or halted is None else halted
        return await self._finish(
            cursor,
            turn_id,
            state,
            user_text,
            final_reply,
            proposals,
            tuple(proposed),
            llm_calls,
            halted,
            None,
        )

    def boundary(self, guard: Callable[[], None] | None, state: _PolicyTurnState) -> None:
        """A safe step boundary (INV-012): a stale worker starts nothing; a restart request
        abandons the turn if nothing irreversible happened, otherwise stops it after reporting."""
        if guard is not None:
            guard()
        if state.cancel is not None and state.cancel():
            if state.effects == 0:
                raise TurnCancelledError("restart requested before any irreversible step")
            raise _StopAfterEffect

    async def _normalize_media(
        self,
        cursor: TurnJournalCursor,
        identity: ConversationIdentity,
        turn_id: str,
        user_text: str,
        media: tuple[MediaReference, ...],
    ) -> str:
        """The turn text including what the media said (journaled: replay never re-transcribes)."""

        async def produce() -> dict[str, Any]:
            context = TranscriptionContext(
                tenant_id=identity.tenant_id,
                conversation_id=identity.conversation_id,
                turn_id=turn_id,
            )
            return {"lines": await self._media.lines(media, context)}

        step = await cursor.step(
            JournalStepType.MEDIA_NORMALIZED, stable_hash([m.media_id for m in media]), produce
        )
        return "\n".join(part for part in (user_text, *step["lines"]) if part)

    def _with_confirmation(self, reply: str, proposal: ProposedAction) -> str:
        line = self._agent.confirmation.prompt.format(
            summary=proposal.request.summary or proposal.request.capability
        )
        return f"{reply}\n\n{line}" if reply else line

    async def _finish(
        self,
        cursor: TurnJournalCursor,
        turn_id: str,
        state: ConversationState,
        user_text: str,
        final_reply: str,
        proposals: dict[str, CapabilityRequest],
        proposed: tuple[ProposedAction, ...],
        llm_calls: int,
        halted: Literal["step_limit", "llm_truncated", "token_budget"] | None,
        reprompt_action_id: str | None,
        *,
        flows: tuple[FlowInstance, ...] | None = None,
        handoff: bool = False,
    ) -> TurnOutcome:
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
            flows=state.flows if flows is None else flows,
        )
        return TurnOutcome(
            turn_id=turn_id,
            reply=final_reply,
            state=new_state,
            llm_calls=llm_calls,
            halted=halted,
            proposed=proposed,
            reprompt_action_id=reprompt_action_id,
            handoff_requested=handoff,
        )

    def _history_messages(self, state: ConversationState) -> list[LLMMessage]:
        window = state.history[-self._agent.max_history_messages :]
        while window and window[0].role != "user":  # providers require a leading user message
            window = window[1:]
        return [LLMMessage.text(m.role, m.text) for m in window]

    async def llm_step(
        self,
        cursor: TurnJournalCursor,
        turn_id: str,
        system: str,
        messages: list[LLMMessage],
        *,
        tools: tuple[LLMToolDefinition, ...] | None = None,
        structured: LLMStructuredOutput | None = None,
        purpose: str = "agent",
    ) -> LLMResponse:
        """One model call, journaled. `purpose` says WHY it is made (agent loop, confirmation
        decision, flow understanding, ...): usage is reported per purpose."""
        request = LLMRequest(
            system=system,
            messages=tuple(messages),
            tools=self._pipeline.exposed_tools() if tools is None else tools,
            structured_output=structured,
        )
        request_hash = llm_request_hash(request)
        llm_request_id = stable_hash(turn_id, cursor.next_index, request_hash)[:32]
        # LLM_REQUEST is persisted *before* the external call (DESIGN §25.1).
        await cursor.step(
            JournalStepType.LLM_REQUEST, request_hash, _const({"llm_request_id": llm_request_id})
        )

        async def call_llm() -> dict[str, Any]:
            # fresh calls only: a replayed step never reaches here
            with bind(purpose=purpose), span("llm.call", purpose=purpose):
                response = await self._llm.complete(
                    request.model_copy(update={"request_id": llm_request_id})
                )
            return response.model_dump(mode="json")

        payload = await cursor.step(JournalStepType.LLM_RESPONSE, request_hash, call_llm)
        return LLMResponse.model_validate(payload)

    async def capability_step(
        self,
        cursor: TurnJournalCursor,
        identity: ConversationIdentity,
        turn_id: str,
        tool_call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        proposals: dict[str, CapabilityRequest],
        proposed: list[ProposedAction],
        guard: Callable[[], None] | None,
        policy_state: _PolicyTurnState,
        reference_time: datetime,
    ) -> ToolResultPart:
        """One LLM tool call: the shared capability path, rendered back to the model."""
        outcome = await self.run_capability(
            cursor,
            identity,
            turn_id,
            self._pipeline.capability_name_for(tool_name),
            arguments,
            proposals,
            proposed,
            guard,
            policy_state,
            reference_time,
        )
        if outcome.proposal is not None:
            return ToolResultPart(
                tool_call_id=tool_call_id, content=render_proposal(outcome.proposal)
            )
        assert outcome.result is not None
        return ToolResultPart(
            tool_call_id=tool_call_id,
            content=render_result(outcome.result),
            is_error=outcome.result.status != "success",
        )

    async def run_capability(
        self,
        cursor: TurnJournalCursor,
        identity: ConversationIdentity,
        turn_id: str,
        capability: str,
        arguments: dict[str, Any],
        proposals: dict[str, CapabilityRequest],
        proposed: list[ProposedAction],
        guard: Callable[[], None] | None,
        policy_state: _PolicyTurnState,
        reference_time: datetime,
    ) -> CapabilityOutcome:
        """The ONE path to a capability, used by the agent loop and by Flows alike: policy
        decision, journal, side-effect protocol, proposal capture. A Flow gets no shortcut."""
        self.boundary(guard, policy_state)  # safe boundary: before PREPARE, never inside it
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
            # Runtime-known facts only; the counters are rebuilt identically on replay.
            ctx = PolicyContext(
                tenant_id=identity.tenant_id,
                ownership=Ownership.BOT,
                tool_calls_this_turn=policy_state.calls,
                recent_args_hashes=tuple(policy_state.hashes),
                now=reference_time,
            )
            return self._pipeline.evaluate(capability, arguments, ctx).model_dump(mode="json")

        evaluation = Evaluation.model_validate(
            await cursor.step(JournalStepType.POLICY_DECISION, request_hash, decide)
        )
        policy_state.calls += 1
        if evaluation.request is not None:
            policy_state.hashes.append(evaluation.request.args_hash)

        context = self._tool_context(identity, turn_id, logical_step_id, evaluation)
        if evaluation.decision.outcome == "allow" and evaluation.request is not None:
            # A real external operation: the configured executor owns the side-effect protocol.
            outcome = await self._executor.run(
                cursor,
                ToolStep(
                    identity=identity,
                    turn_id=turn_id,
                    logical_step_id=logical_step_id,
                    request_hash=request_hash,
                    evaluation=evaluation,
                    context=context,
                    guard=guard,
                ),
            )
        else:  # denied / invalid / proposal: no external operation, nothing to ledger

            async def immediate() -> dict[str, Any]:
                return (await self._pipeline.execute(evaluation, context)).model_dump(mode="json")

            outcome = CapabilityOutcome.model_validate(
                await cursor.step(
                    JournalStepType.TOOL_RESULT,
                    request_hash,
                    immediate,
                    logical_step_id=logical_step_id,
                )
            )
        executed = evaluation.decision.outcome == "allow" and evaluation.request is not None
        if executed and self._pipeline.resolve(capability).effective_risk != "read":
            policy_state.effects += 1  # from here a restart can no longer abandon the turn
        if outcome.proposal is not None:
            proposals[outcome.proposal.capability] = outcome.proposal
            proposed.append(
                ProposedAction(
                    request=outcome.proposal,
                    tool_name=self._pipeline.resolve(outcome.proposal.capability).tool.name,
                )
            )
        return outcome

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
            trace_id=trace_id_for(turn_id),
        )


class _EngineFlowIO:
    """Gives the FlowRunner exactly one thing: the engine's capability path."""

    def __init__(
        self,
        engine: TurnEngine,
        cursor: TurnJournalCursor,
        identity: ConversationIdentity,
        turn_id: str,
        proposals: dict[str, CapabilityRequest],
        proposed: list[ProposedAction],
        guard: Callable[[], None] | None,
        policy_state: _PolicyTurnState,
        reference_time: datetime,
        system: str,
        history: list[LLMMessage],
        user_text: str,
        catalog: tuple[LLMToolDefinition, ...],
    ) -> None:
        self._engine = engine
        self._args = (cursor, identity, turn_id)
        self._rest = (proposals, proposed, guard, policy_state, reference_time)
        self._model = (system, history, user_text, catalog)

    async def extract(
        self, system: str, message: str, schema: LLMStructuredOutput
    ) -> dict[str, Any]:
        cursor, _, turn_id = self._args
        self._engine.boundary(self._rest[2], self._rest[3])
        response = await self._engine.llm_step(
            cursor,
            turn_id,
            system,
            [LLMMessage.text("user", message)],
            tools=(),
            structured=schema,
            purpose="flow_understanding",
        )
        return response.structured or {}

    async def digress(self, note: str, text: str, capabilities: tuple[str, ...]) -> str:
        """A bounded agent loop that can only read: the tools offered are the flow's allowed
        read capabilities, and anything else the model asks for is refused here too."""
        engine = self._engine
        cursor, identity, turn_id = self._args
        proposals, proposed, guard, policy_state, reference_time = self._rest
        system, history, user_text, catalog = self._model
        allowed = {
            tool.name
            for tool in catalog
            if engine.pipeline.capability_name_for(tool.name) in capabilities
        }
        tools = tuple(t for t in catalog if t.name in allowed)
        working = [*history, LLMMessage.text("user", user_text)]
        for _ in range(3):
            engine.boundary(guard, policy_state)
            response = await engine.llm_step(
                cursor,
                turn_id,
                f"{system}\n\n{note}",
                working,
                tools=tools,
                purpose="flow_digression",
            )
            if response.stop_reason is LLMStopReason.MAX_TOKENS:
                break
            if not response.tool_calls:
                return response.text.strip() or engine.agent.fallback_reply
            working.append(LLMMessage(role="assistant", parts=response.parts))
            parts: list[ToolResultPart] = []
            for call in response.tool_calls:
                if call.name not in allowed:
                    parts.append(
                        ToolResultPart(
                            tool_call_id=call.id,
                            content='{"status": "policy_denied"}',
                            is_error=True,
                        )
                    )
                    continue
                parts.append(
                    await engine.capability_step(
                        cursor,
                        identity,
                        turn_id,
                        call.id,
                        call.name,
                        call.arguments,
                        proposals,
                        proposed,
                        guard,
                        policy_state,
                        reference_time,
                    )
                )
            working.append(LLMMessage(role="user", parts=tuple(parts)))
        return engine.agent.fallback_reply

    async def invoke(self, capability: str, args: dict[str, Any]) -> CapabilityOutcome:
        cursor, identity, turn_id = self._args
        proposals, proposed, guard, policy_state, reference_time = self._rest
        return await self._engine.run_capability(
            cursor, identity, turn_id, capability, args, proposals, proposed, guard,
            policy_state, reference_time,
        )  # fmt: skip


class _StopAfterEffect(Exception):
    """Internal: cancellation was requested after an irreversible operation had happened."""


class _PolicyTurnState:
    """Per-turn counters feeding the PolicyGate (tool-call budget, loop detection) and the
    cooperative-cancellation bookkeeping (effects performed so far)."""

    def __init__(self, cancel: Callable[[], bool] | None = None) -> None:
        self.calls = 0
        self.hashes: list[str] = []
        self.effects = 0
        self.cancel = cancel


def _const(payload: dict[str, Any]) -> Callable[[], Awaitable[dict[str, Any]]]:
    async def produce() -> dict[str, Any]:
        return payload

    return produce
