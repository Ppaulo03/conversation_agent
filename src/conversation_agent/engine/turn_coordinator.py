"""TurnCoordinator: the normative turn algorithm (DESIGN §39D) over the reliability ports.

  candidate -> conversation lease -> claim events (inside the lease) -> turn + journal
  -> ownership gate -> engine -> one local transaction {state, outbox, turn, inbox consumed}
  -> release lease

No database transaction is open while the LLM or an external system is being called: each
write is a short fenced UoW. A crash at any point leaves the turn open; the next owner
(higher epoch) resumes the same turn and replays its journal.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

from conversation_agent.core.canonical import stable_hash
from conversation_agent.core.compiler import CompiledAgent
from conversation_agent.core.errors import (
    AgentVersionUnavailableError,
    FencingError,
    JournalDivergenceError,
    LLMProviderError,
    StaleWorkerError,
    ToolResultPendingError,
    TurnCancelledError,
)
from conversation_agent.core.models.actions import PendingAction
from conversation_agent.core.models.conversation import (
    ConversationMessage,
    ConversationState,
    TurnOutcome,
)
from conversation_agent.core.models.journal import JournalStepType
from conversation_agent.core.models.runtime import (
    ConversationKey,
    FenceToken,
    InvocationStatus,
    OpenedTurn,
    OutboundMessage,
    OutboxStatus,
    Ownership,
    ToolInvocation,
)
from conversation_agent.core.redaction import redact
from conversation_agent.core.versioning import Version
from conversation_agent.engine.heartbeat import LeaseHandle
from conversation_agent.engine.journal_steps import TurnJournalCursor
from conversation_agent.engine.turn_engine import TurnEngine
from conversation_agent.ports.clock import Clock
from conversation_agent.ports.faults import FaultInjector
from conversation_agent.ports.inbox import InboxStore
from conversation_agent.ports.journal import TurnJournal
from conversation_agent.ports.lease import ConversationLeaseStore
from conversation_agent.ports.registry import AgentRegistry
from conversation_agent.ports.uow import (
    ConversationUnitOfWork,
    ConversationUnitOfWorkFactory,
    StoredConversation,
)

log = logging.getLogger(__name__)

RunStatus = Literal["idle", "busy", "done", "retry_later", "waiting", "stale"]


@dataclass(frozen=True)
class ConversationRun:
    status: RunStatus
    turns_completed: int = 0


def default_side_effect_notice(invocations: list[ToolInvocation]) -> str | None:
    """Deterministic message for a turn that failed after real external effects."""
    if not invocations:
        return None
    ref = invocations[0].invocation_id[:8]
    if any(
        i.status in (InvocationStatus.SUCCEEDED, InvocationStatus.RECONCILED) for i in invocations
    ):
        return f"Your request was carried out, but I could not compose the full reply. Ref: {ref}."
    return f"I could not confirm the outcome of your request; a person will check it. Ref: {ref}."


class TurnCoordinator:
    def __init__(
        self,
        *,
        owner: str,
        leases: ConversationLeaseStore,
        uows: ConversationUnitOfWorkFactory,
        inbox: InboxStore,
        journal_factory: Callable[[FenceToken], TurnJournal],
        engine_factory: Callable[[FenceToken, TurnJournal], TurnEngine],
        clock: Clock,
        faults: FaultInjector,
        lease_ttl: timedelta = timedelta(seconds=30),
        heartbeat_interval_seconds: float = 10.0,
        max_turn_attempts: int = 5,
        side_effect_notice: Callable[[list[ToolInvocation]], str | None] | None = None,
        confirmation_ttl: timedelta = timedelta(minutes=30),
        registry: AgentRegistry | None = None,
        agent_id: str | None = None,
        versioned_engine_factory: Callable[[FenceToken, TurnJournal, CompiledAgent], TurnEngine]
        | None = None,
    ) -> None:
        if (registry is None) != (versioned_engine_factory is None) or (
            registry is not None and agent_id is None
        ):
            raise ValueError("registry, agent_id and versioned_engine_factory go together")
        self._registry = registry
        self._agent_id = agent_id
        self._versioned_engine_factory = versioned_engine_factory
        self._confirmation_ttl = confirmation_ttl
        self._side_effect_notice = side_effect_notice or default_side_effect_notice
        self._owner = owner
        self._leases = leases
        self._uows = uows
        self._inbox = inbox
        self._journal_factory = journal_factory
        self._engine_factory = engine_factory
        self._clock = clock
        self._faults = faults
        self._ttl = lease_ttl
        self._interval = heartbeat_interval_seconds
        self._max_attempts = max_turn_attempts

    async def run_once(self, limit: int = 50) -> list[ConversationRun]:
        """One polling pass: candidates are chosen without claiming anything; the claim
        happens only after the conversation lease is held."""
        candidates = await self._inbox.list_ready_conversations(limit)
        return [await self.process_conversation(key) for key in candidates]

    async def process_conversation(self, key: ConversationKey) -> ConversationRun:
        lease = await self._leases.acquire(key, self._owner, self._ttl)
        if lease is None:
            return ConversationRun("busy")  # nothing is claimed by the losing worker
        handle = LeaseHandle(
            lease, self._leases, self._clock, ttl=self._ttl, interval_seconds=self._interval
        )
        await handle.start()
        completed = 0
        try:
            while True:
                handle.ensure_active()
                step = await self._process_next_turn(handle)
                if step == "none":
                    break
                if step == "waiting":
                    # Not a failure: a tool outcome is pending (executing/unknown/reconciling).
                    # Nothing is counted, nothing is re-run; the turn resumes once it is known.
                    await self._leases.release(handle.lease)
                    return ConversationRun("waiting", completed)
                if step == "retry":
                    # A handled failure (not a crash): give the conversation back so the
                    # next pass can resume this open turn from its journal.
                    await self._leases.release(handle.lease)
                    return ConversationRun("retry_later", completed)
                if step == "cancelled":
                    continue  # the turn was restarted: not completed, look at the inbox again
                completed += 1
            await self._leases.release(handle.lease)
            return ConversationRun("done" if completed else "idle", completed)
        except (StaleWorkerError, FencingError):
            # We no longer own the conversation: do not touch it (and do not release a lease
            # that is not ours). The new owner resumes from the journal/ledger.
            return ConversationRun("stale", completed)
        finally:
            await handle.stop()

    async def _process_next_turn(
        self, handle: LeaseHandle
    ) -> Literal["none", "completed", "retry", "waiting", "cancelled"]:
        fence = handle.fence
        async with self._uows.begin(fence) as uow:
            stored = await uow.state.load()
            opened = await uow.turns.open_next(self._owner, self._clock.now())
            pending = None
            if opened is not None:
                # A resumed turn must continue in the stage it already journaled, even if its
                # action has since left PENDING_CONFIRMATION (e.g. CONFIRMED before a crash).
                decided = await uow.journal.find_step(
                    opened.turn_id, JournalStepType.CONFIRMATION_DECISION
                )
                pending = (
                    await uow.actions.get(decided.payload["action_id"])
                    if decided is not None
                    else await uow.actions.awaiting()
                )
            await uow.commit()
        if opened is None:
            return "none"

        journal = self._journal_factory(fence)
        if stored.ownership is not Ownership.BOT:
            await self._record_silent_turn(fence, journal, opened, stored.state, stored.ownership)
            return "completed"

        try:
            if self._registry is not None and self._versioned_engine_factory is not None:
                compiled = await self._resolve_agent(fence, stored, pending, opened)
                engine = self._versioned_engine_factory(fence, journal, compiled)
            else:
                engine = self._engine_factory(fence, journal)
        except AgentVersionUnavailableError as exc:
            # Never run a conversation on a version it was not pinned to: wait for an operator.
            log.error("ALERT agent version unavailable, turn left open: %s", redact(str(exc)))
            return "retry"
        try:
            outcome = await engine.process_turn(
                opened.identity,
                stored.state,
                opened.user_text,
                opened.turn_id,
                guard=handle.ensure_active,
                late_event_ids=opened.late_event_ids,
                pending=pending,
                inbound=opened.inbound,
                cancel=lambda: handle.cancel_requested,
            )
        except TurnCancelledError:
            # A newer message asked to restart and nothing irreversible had happened: abandon
            # this turn at the safe boundary and let the next one aggregate the new message.
            async with self._uows.begin(fence) as uow:
                await uow.turns.cancel(opened.turn_id)
                await uow.commit()
            handle.acknowledge_cancel()
            return "cancelled"
        except JournalDivergenceError as exc:
            log.error("ALERT journal divergence, turn failed closed: %s", redact(str(exc)))
            await self._fail_turn(fence, opened, f"journal_divergence: {exc}")
            return "completed"
        except ToolResultPendingError:
            return "waiting"
        except LLMProviderError as exc:
            async with self._uows.begin(fence) as uow:
                failures = await uow.turns.record_failure(opened.turn_id)
                await uow.commit()
            if failures >= self._max_attempts:
                log.error(
                    "ALERT turn %s failed after %s attempts: %s",
                    opened.turn_id,
                    failures,
                    redact(str(exc)),
                )
                await self._fail_turn(fence, opened, f"{type(exc).__name__}: {exc}")
                return "completed"
            return "retry"  # the turn stays open; the next pass resumes it from the journal

        await self._faults.hit("C16_after_apply_before_compose")
        async with self._uows.begin(fence) as uow:
            await uow.state.save(outcome.state, last_event_at=opened.last_event_at)
            await self._persist_reply(uow, opened, outcome)
            await uow.turns.complete(opened.turn_id)
            await uow.inbox.consume(opened.event_ids)
            await uow.commit()
        handle.acknowledge_cancel()  # completing the turn answered any restart request
        return "completed"

    async def _resolve_agent(
        self,
        fence: FenceToken,
        stored: StoredConversation,
        pending: PendingAction | None,
        opened: OpenedTurn,
    ) -> CompiledAgent:
        """The agent version this turn runs on. A conversation stays on its pinned version for
        as long as anything is in progress (a flow, a pending confirmation, a resumed turn);
        only when it is idle does it move to the latest published one."""
        assert self._registry is not None and self._agent_id is not None
        pinned = stored.agent_version
        target = pinned
        idle = not stored.state.flows and pending is None and not opened.resumed
        if pinned is None or idle:
            latest = await self._registry.latest(self._agent_id)
            if latest is not None and (pinned is None or Version(latest.version) > Version(pinned)):
                target = latest.version
        if target is None:
            raise AgentVersionUnavailableError(f"no published version of {self._agent_id!r}")
        if target != pinned:
            async with self._uows.begin(fence) as uow:
                await uow.state.pin_agent_version(target)
                await uow.commit()
        compiled = await self._registry.get(self._agent_id, target)
        if compiled is None:
            raise AgentVersionUnavailableError(f"{self._agent_id!r} {target} is not published")
        return compiled

    async def _persist_reply(
        self, uow: ConversationUnitOfWork, opened: OpenedTurn, outcome: TurnOutcome
    ) -> None:
        """The reply, and - in the SAME transaction - the PendingAction it asks the user to
        confirm (or the re-prompt of an existing one), so the prompt row and the action it
        refers to can never disagree (DESIGN §26.1)."""
        message = self._outbound(opened, outcome.reply)
        action_id: str | None = None
        new_attempt = False
        if outcome.proposed:
            proposal = outcome.proposed[-1]  # a newer proposal supersedes earlier ones
            identity = opened.identity
            request = proposal.request
            action = PendingAction(
                tenant_id=identity.tenant_id,
                conversation_id=identity.conversation_id,
                action_id=stable_hash(
                    identity.tenant_id,
                    identity.conversation_id,
                    "action",
                    opened.turn_id,
                    request.capability,
                    request.args_hash,
                )[:32],
                capability=request.capability,
                tool_name=proposal.tool_name,
                request=request,
                args_hash=request.args_hash,
                protected_fields=tuple(sorted(request.args)),  # every argument is protected
                summary=request.summary or request.capability,
                created_from_turn=opened.turn_id,
                expires_at=self._clock.now() + self._confirmation_ttl,
            )
            stored, created = await uow.actions.create_or_reuse(action)
            action_id, new_attempt = stored.action_id, not created
        elif outcome.reprompt_action_id is not None:
            action_id, new_attempt = outcome.reprompt_action_id, True
        if action_id is None:
            await uow.outbox.add(message)
            return
        message = message.model_copy(update={"action_id": action_id})
        for prompt in await uow.actions.prompts(action_id):
            if prompt.status is OutboxStatus.UNKNOWN:  # replaced: no longer eligible
                await uow.outbox.supersede(prompt.outbox_id)
        await uow.outbox.add(message)
        await uow.actions.set_prompt(action_id, message.outbox_id, new_attempt=new_attempt)

    async def _record_silent_turn(
        self,
        fence: FenceToken,
        journal: TurnJournal,
        opened: OpenedTurn,
        state: ConversationState,
        ownership: Ownership,
    ) -> None:
        """INV-019: a conversation not owned by the bot gets context persisted and nothing
        else: no Router, no LLM, no tool, no outbound."""
        cursor = TurnJournalCursor(journal, opened.turn_id)

        async def check() -> dict[str, object]:
            return {"ownership": ownership.value, "event_ids": list(opened.event_ids)}

        await cursor.step(JournalStepType.OWNERSHIP_CHECK, stable_hash(opened.user_text), check)
        new_state = state.model_copy(
            update={
                "history": (*state.history, ConversationMessage(role="user", text=opened.user_text))
            }
        )
        async with self._uows.begin(fence) as uow:
            await uow.state.save(new_state, last_event_at=opened.last_event_at)
            await uow.turns.complete(opened.turn_id)
            await uow.inbox.consume(opened.event_ids)
            await uow.commit()

    async def _fail_turn(self, fence: FenceToken, opened: OpenedTurn, reason: str) -> None:
        async with self._uows.begin(fence) as uow:
            await uow.turns.fail(opened.turn_id, reason)
            await uow.inbox.dead(opened.event_ids)
            # A turn that dies AFTER an external side effect must still tell the contact
            # something deterministic: never dead-letter silently behind a real action.
            notice = self._side_effect_notice(await uow.invocations.for_turn(opened.turn_id))
            if notice is not None:
                await uow.outbox.add(self._outbound(opened, notice, index=1))
            await uow.commit()

    @staticmethod
    def _outbound(opened: OpenedTurn, text: str, index: int = 0) -> OutboundMessage:
        identity = opened.identity
        key = stable_hash(identity.tenant_id, identity.conversation_id, opened.turn_id, index)
        return OutboundMessage(
            outbox_id=key[:32],
            tenant_id=identity.tenant_id,
            conversation_id=identity.conversation_id,
            channel_id=identity.channel_id,
            contact_id=identity.contact_id,
            turn_id=opened.turn_id,
            message_index=index,
            text=text,
            idempotency_key=key,
        )
