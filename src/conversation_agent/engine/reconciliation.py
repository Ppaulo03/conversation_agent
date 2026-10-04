"""Reconciliation of `UNKNOWN` tool invocations (DESIGN §13-14, INV-006, INV-021).

An unknown outcome is never retried blindly. The tool's declared recovery contract decides:

  status_lookup    ask the external system what happened (a read capability);
                   found -> RECONCILED with that result; "no such operation" -> the write
                   provably did not happen, so (idempotent tools only) re-send with the SAME key;
                   lookup unavailable -> stay UNKNOWN and retry later (durable timer)
  retry_same_key   re-send with the same idempotency key
  human_handoff    escalate (also the default for tools with no contract, and after too many
                   attempts)

Every transition is fenced by the claim's `execution_epoch`: a worker killed mid-way (C11) is
superseded by whoever claims next, and its late writes are refused.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from pydantic import ValidationError

from conversation_agent.core.errors import ExecutionFencingError, MappingError
from conversation_agent.core.models.runtime import (
    ExecutionClaim,
    InvocationStatus,
    ScheduledEvent,
    ToolInvocation,
)
from conversation_agent.core.models.tooling import ToolError, ToolResult
from conversation_agent.engine.capability_pipeline import CapabilityPipeline
from conversation_agent.engine.side_effects import to_tool_result
from conversation_agent.ports.clock import Clock
from conversation_agent.ports.faults import FaultInjector
from conversation_agent.ports.ledger import ToolInvocationStore
from conversation_agent.ports.scheduler import Scheduler
from conversation_agent.tools.mapping import apply_mapping

RECONCILE_EVENT = "reconcile"


def reconcile_key(invocation_id: str) -> str:
    return f"reconcile:{invocation_id}"


class ReconciliationWorker:
    def __init__(
        self,
        *,
        ledger: ToolInvocationStore,
        pipeline: CapabilityPipeline,
        scheduler: Scheduler,
        faults: FaultInjector,
        clock: Clock,
        owner: str,
        claim_ttl: timedelta = timedelta(seconds=60),
        retry_backoff: timedelta = timedelta(seconds=30),
        max_attempts: int = 5,
    ) -> None:
        self._ledger = ledger
        self._pipeline = pipeline
        self._scheduler = scheduler
        self._faults = faults
        self._clock = clock
        self._owner = owner
        self._ttl = claim_ttl
        self._backoff = retry_backoff
        self._max_attempts = max_attempts

    async def run_once(self, limit: int = 10) -> list[ToolInvocation]:
        resolved: list[ToolInvocation] = []
        for invocation, claim in await self._ledger.claim_reconciliation(
            self._owner, limit, self._ttl, agent_id=self._pipeline.agent_id
        ):
            await self._faults.hit("C11_during_reconciliation")  # claimed, nothing persisted yet
            result, handoff = await self._resolve(invocation)
            try:
                final = await self._ledger.finalize_reconciliation(
                    invocation.tenant_id, invocation.invocation_id, claim, result, handoff=handoff
                )
            except ExecutionFencingError:
                continue  # superseded by a newer claim: whoever holds it decides
            if final.status is InvocationStatus.UNKNOWN:
                await self._schedule_retry(final)
            resolved.append(final)
        return resolved

    async def _schedule_retry(self, invocation: ToolInvocation) -> None:
        await self._scheduler.schedule(
            ScheduledEvent(
                tenant_id=invocation.tenant_id,
                scheduler_key=reconcile_key(invocation.invocation_id),
                event_type=RECONCILE_EVENT,
                due_at=self._clock.now() + self._backoff,
                payload={"invocation_id": invocation.invocation_id},
            )
        )

    async def _resolve(self, invocation: ToolInvocation) -> tuple[ToolResult | None, bool]:
        """Returns (result, handoff). (None, False) means "still unknown, try again later".

        Decisions come from the recovery contract FROZEN at PREPARE (INV-023), and any re-send
        executes the frozen operation, never one re-derived from today's definitions."""
        intent = invocation.intent
        recovery = intent.recovery
        if invocation.reconcile_attempts > self._max_attempts:
            return self._handoff("RECONCILIATION_EXHAUSTED"), True
        if recovery.strategy == "human_handoff":
            return self._handoff("NO_AUTOMATIC_RECOVERY"), True

        if recovery.strategy == "status_lookup":
            lookup = recovery.lookup_intent
            if lookup is None:
                return self._handoff("LOOKUP_NOT_FROZEN"), True
            if not self._pipeline.intent_matches(lookup):
                return self._handoff("LOOKUP_CHANGED"), True  # never ask a different system
            # The FROZEN lookup: same tool args, same destination as when the write was prepared.
            found = await self._pipeline.run_frozen(lookup, invocation.context)
            if found.error is not None and found.error.code == "DESTINATION_CHANGED":
                return self._handoff("LOOKUP_DESTINATION_CHANGED"), True
            if found.status == "success":
                adopted = self._adopt(invocation, found.data)
                if adopted is None:
                    return self._handoff("LOOKUP_RESULT_UNUSABLE"), True
                return adopted, False  # it DID happen: adopt the recorded result
            proves_absent = (
                found.status == "business_error"
                and found.error is not None
                and found.error.code in recovery.absent_codes
            )
            if not proves_absent:
                # Lookup failed, or answered with an error that is NOT proof of absence
                # (e.g. "account suspended"): the outcome is still unknown.
                return None, False
            if not recovery.idempotency_supported:
                return self._handoff("NOT_FOUND_BUT_NOT_IDEMPOTENT"), True
        elif recovery.strategy == "retry_same_key" and not recovery.idempotency_supported:
            return self._handoff("NOT_IDEMPOTENT"), True

        return await self._resend_frozen(invocation)

    def _adopt(
        self, invocation: ToolInvocation, lookup_data: dict[str, Any] | None
    ) -> ToolResult | None:
        """Lookup output -> the ORIGINAL capability's result, via the frozen `result_map` (or
        directly when the two schemas are identical). Anything that does not fit is refused."""
        capability = self._pipeline.resolve(invocation.capability).capability
        recovery = invocation.intent.recovery
        try:
            data: dict[str, Any] = lookup_data or {}
            if recovery.result_map is not None:
                data = apply_mapping(recovery.result_map, data)
            validated = capability.output_model.model_validate(data).model_dump(mode="json")
        except (MappingError, ValidationError):
            return None
        return ToolResult(status="success", data=validated)

    async def _resend_frozen(self, invocation: ToolInvocation) -> tuple[ToolResult | None, bool]:
        """Same frozen tool args, same destination, same idempotency key (INV-021/023/027)."""
        if not self._pipeline.intent_matches(invocation.intent):
            return self._handoff("INTENT_CHANGED"), True  # definitions moved on: never guess
        result = await self._pipeline.run_frozen(invocation.intent, invocation.context)
        if result.error is not None and result.error.code == "DESTINATION_CHANGED":
            return self._handoff("DESTINATION_CHANGED"), True  # configuration moved: never guess
        return to_tool_result(result), False

    @staticmethod
    def _handoff(code: str) -> ToolResult:
        return ToolResult(
            status="unknown",
            error=ToolError(
                code=code, message_safe="The outcome could not be confirmed automatically."
            ),
        )


def claim_of(invocation: ToolInvocation) -> ExecutionClaim:  # test/ops convenience
    return ExecutionClaim(invocation_id=invocation.invocation_id, epoch=invocation.execution_epoch)
