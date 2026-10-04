"""Tool-step executors: how an ALLOWed capability call reaches the external system.

DirectToolExecutor   Phase 1 behaviour: run and journal the result (reads, in-memory).
LedgerToolExecutor   RUNTIME_PROTOCOL §4, with the two independent fences:

  A.  PREPARE   (conversation_epoch)  freeze the concrete operation (INV-023), then
                                       invocation(PREPARED) + TOOL_PREPARED, one transaction
  B.  EXECUTE   (execution_epoch)     claim -> EXECUTING -> provider call with the FROZEN args
  C1. FINALIZE  (execution_epoch)     SUCCEEDED | FAILED | UNKNOWN in the ledger
  C2. APPLY     (conversation_epoch)  mark applied + TOOL_RESULT, one transaction

No database transaction is open during B. If the conversation is lost after C1 the fact stays
in the ledger and the next owner applies it (C2) without calling the provider again.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol

from conversation_agent.core.errors import ExecutionFencingError, ToolResultPendingError
from conversation_agent.core.models.conversation import ConversationIdentity
from conversation_agent.core.models.journal import JournalEntry, JournalStepType
from conversation_agent.core.models.runtime import (
    TERMINAL_INVOCATION_STATUSES,
    ExecutionIntent,
    FenceToken,
    InvocationStatus,
    ToolInvocation,
)
from conversation_agent.core.models.tooling import (
    CapabilityRequest,
    CapabilityResult,
    ToolContext,
    ToolError,
    ToolResult,
)
from conversation_agent.engine.capability_pipeline import (
    CapabilityOutcome,
    CapabilityPipeline,
    Evaluation,
)
from conversation_agent.engine.journal_steps import TurnJournalCursor
from conversation_agent.ports.clock import Clock
from conversation_agent.ports.faults import FaultInjector
from conversation_agent.ports.ledger import ToolInvocationStore
from conversation_agent.ports.uow import ConversationUnitOfWorkFactory


@dataclass(frozen=True)
class ToolStep:
    identity: ConversationIdentity
    turn_id: str
    logical_step_id: str
    request_hash: str
    evaluation: Evaluation  # decision == allow and a validated request
    context: ToolContext
    guard: Callable[[], None] | None = None


class ToolStepExecutor(Protocol):
    async def run(self, cursor: TurnJournalCursor, step: ToolStep) -> CapabilityOutcome: ...


class DirectToolExecutor:
    def __init__(self, pipeline: CapabilityPipeline) -> None:
        self._pipeline = pipeline

    async def run(self, cursor: TurnJournalCursor, step: ToolStep) -> CapabilityOutcome:
        async def execute() -> dict[str, Any]:
            outcome = await self._pipeline.execute(step.evaluation, step.context)
            return outcome.model_dump(mode="json")

        payload = await cursor.step(
            JournalStepType.TOOL_RESULT,
            step.request_hash,
            execute,
            logical_step_id=step.logical_step_id,
        )
        return CapabilityOutcome.model_validate(payload)


def to_tool_result(result: CapabilityResult) -> ToolResult:
    return ToolResult(
        status=result.status,
        data=result.data,
        error=result.error,
        provider_metadata=result.provider_metadata,
    )


def to_capability_result(invocation: ToolInvocation) -> CapabilityResult:
    """The terminal ledger fact, as the conversation sees it (capability schema)."""
    if invocation.result is None:
        return CapabilityResult(
            status="unknown",
            error=ToolError(code="NO_RECORDED_RESULT", message_safe="Outcome not recorded."),
        )
    stored = ToolResult.model_validate(invocation.result)
    return CapabilityResult(
        status=stored.status,
        data=stored.data if isinstance(stored.data, dict) else None,
        error=stored.error,
        provider_metadata=stored.provider_metadata,
    )


class LedgerToolExecutor:
    def __init__(
        self,
        *,
        pipeline: CapabilityPipeline,
        uows: ConversationUnitOfWorkFactory,
        ledger: ToolInvocationStore,
        fence: FenceToken,
        faults: FaultInjector,
        clock: Clock,
        owner: str,
        execution_ttl: timedelta = timedelta(seconds=60),
    ) -> None:
        self._pipeline = pipeline
        self._uows = uows
        self._ledger = ledger
        self._fence = fence
        self._faults = faults
        self._clock = clock
        self._owner = owner
        self._ttl = execution_ttl

    async def run(self, cursor: TurnJournalCursor, step: ToolStep) -> CapabilityOutcome:
        invocation_id = step.context.invocation_id
        request = step.evaluation.request
        assert request is not None
        frozen: list[ExecutionIntent] = []  # filled only if PREPARE is not already journaled

        async def prepared_payload() -> dict[str, Any]:
            # INV-023/027: the operation, its destination and its recovery are decided ONCE, here.
            intent = await self._pipeline.prepare_intent(request, step.context)
            if isinstance(intent, CapabilityResult):
                # A binding defect: nothing is prepared, nothing can ever be sent.
                failure = CapabilityOutcome(result=intent).model_dump(mode="json")
                return {"invocation_id": None, "pre_io_failure": failure}
            frozen.append(intent)
            return {"invocation_id": invocation_id}

        async def write_prepare(entry: JournalEntry) -> None:  # A: one transaction
            async with self._uows.begin(self._fence) as uow:
                if frozen:
                    await uow.invocations.create_prepared(
                        new_invocation(
                            step.context,
                            step.logical_step_id,
                            request,
                            frozen[0],
                            attempt=f"{step.logical_step_id}:a1",
                        )
                    )
                await uow.journal.append(entry)
                await uow.commit()

        prepared = await cursor.step_atomic(
            JournalStepType.TOOL_PREPARED,
            step.request_hash,
            prepared_payload,
            write_prepare,
            logical_step_id=step.logical_step_id,
        )
        if prepared.get("pre_io_failure") is not None:
            return CapabilityOutcome.model_validate(prepared["pre_io_failure"])

        return await self.apply_prepared(
            cursor,
            invocation_id=invocation_id,
            request_hash=step.request_hash,
            logical_step_id=step.logical_step_id,
            guard=step.guard,
        )

    async def apply_prepared(
        self,
        cursor: TurnJournalCursor,
        *,
        invocation_id: str,
        request_hash: str | None,
        logical_step_id: str | None,
        guard: Callable[[], None] | None,
    ) -> CapabilityOutcome:
        """B + C1 + C2 for an invocation that is already PREPARED (by `run` for unprotected
        calls, or by the atomic confirmation transaction for protected actions)."""

        async def result_payload() -> dict[str, Any]:  # only runs when TOOL_RESULT is not journaled
            invocation = await self._ledger.get(self._fence.tenant_id, invocation_id)
            assert invocation is not None, "PREPARED invocation must exist"
            terminal = await self._drive(invocation, guard)
            outcome = CapabilityOutcome(result=to_capability_result(terminal))
            return outcome.model_dump(mode="json")

        async def write_apply(entry: JournalEntry) -> None:  # C2: one transaction
            async with self._uows.begin(self._fence) as uow:
                await uow.invocations.mark_applied(invocation_id, self._clock.now())
                await uow.journal.append(entry)
                await uow.commit()

        payload = await cursor.step_atomic(
            JournalStepType.TOOL_RESULT,
            request_hash,
            result_payload,
            write_apply,
            logical_step_id=logical_step_id,
        )
        return CapabilityOutcome.model_validate(payload)

    async def _drive(
        self, invocation: ToolInvocation, guard: Callable[[], None] | None
    ) -> ToolInvocation:
        """B + C1 when the invocation is still PREPARED; otherwise reuse the recorded fact."""
        tenant = self._fence.tenant_id
        while True:
            if invocation.status in TERMINAL_INVOCATION_STATUSES:
                return invocation  # C07/C14 recovery: the fact exists, apply it, never re-run
            if invocation.status is not InvocationStatus.PREPARED:
                # EXECUTING elsewhere, UNKNOWN or RECONCILING: the outcome is not known yet.
                raise ToolResultPendingError(
                    f"invocation {invocation.invocation_id} is {invocation.status}"
                )
            if guard is not None:
                guard()  # safe boundary: a stale worker starts no new external call
            claim = await self._ledger.claim_execution(
                tenant, invocation.invocation_id, self._owner, self._ttl
            )
            if claim is None:  # raced with another executor
                reloaded = await self._ledger.get(tenant, invocation.invocation_id)
                assert reloaded is not None
                invocation = reloaded
                continue

            await self._faults.hit("C04_before_external_request")
            if not self._pipeline.intent_matches(invocation.intent):
                # Prepared under other definitions (deploy between PREPARE and EXECUTE): the
                # frozen operation is no longer what this build would send. Nothing has been
                # sent, so this is a known, terminal non-execution.
                result: CapabilityResult = CapabilityResult(
                    status="technical_error",
                    error=ToolError(
                        code="INTENT_CHANGED",
                        message_safe="The prepared operation no longer matches the deployed "
                        "definitions; it was not executed.",
                    ),
                )
            else:
                result = await self._run_frozen(tenant, invocation, claim)
            await self._faults.hit("C06_after_external_success")
            try:  # C1: independent of the conversation lease (INV-011)
                invocation = await self._ledger.finalize_execution(
                    tenant, invocation.invocation_id, claim, to_tool_result(result)
                )
            except ExecutionFencingError as exc:
                raise ToolResultPendingError(
                    str(exc)
                ) from exc  # taken over: reconciliation decides
            await self._faults.hit("C07_after_ledger_finalize")

    async def _run_frozen(
        self, tenant: str, invocation: ToolInvocation, claim: Any
    ) -> CapabilityResult:
        renewer = asyncio.create_task(self._renew(tenant, invocation.invocation_id, claim))
        try:
            # The FROZEN tool args and the stored context (same idempotency key) - never a
            # re-mapped request.
            return await self._pipeline.run_frozen(invocation.intent, invocation.context)
        finally:
            renewer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await renewer

    async def _renew(self, tenant: str, invocation_id: str, claim: Any) -> None:
        interval = self._ttl.total_seconds() / 3
        while True:
            await asyncio.sleep(interval)
            with contextlib.suppress(Exception):
                await self._ledger.renew_execution(tenant, invocation_id, claim, self._ttl)


def new_invocation(
    ctx: ToolContext,
    logical_step_id: str,
    request: CapabilityRequest,
    intent: ExecutionIntent,
    *,
    attempt: str,
    action_id: str | None = None,
) -> ToolInvocation:
    return ToolInvocation(
        tenant_id=ctx.tenant_id,
        invocation_id=ctx.invocation_id,
        conversation_id=ctx.conversation_id,
        session_id=ctx.session_id,
        turn_id=ctx.turn_id,
        logical_step_id=logical_step_id,
        attempt_semantic_id=attempt,
        action_id=action_id,
        tool_name=intent.tool_name,
        capability=request.capability,
        args_hash=request.args_hash,
        idempotency_key=ctx.invocation_id,
        request=request,
        context=ctx,
        intent=intent,
    )
