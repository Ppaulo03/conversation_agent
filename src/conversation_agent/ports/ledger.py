from __future__ import annotations

from datetime import timedelta
from typing import Protocol

from conversation_agent.core.models.runtime import ExecutionClaim, ToolInvocation
from conversation_agent.core.models.tooling import ToolResult


class ToolInvocationStore(Protocol):
    """Execution-side ledger transitions, fenced by `execution_epoch` only (INV-011, INV-016).

    None of these needs the conversation lease: losing the conversation never erases an
    external fact.
    """

    async def get(self, tenant_id: str, invocation_id: str) -> ToolInvocation | None: ...

    async def claim_execution(
        self, tenant_id: str, invocation_id: str, owner: str, ttl: timedelta
    ) -> ExecutionClaim | None:
        """PREPARED -> EXECUTING with a new execution_epoch. None if not PREPARED."""
        ...

    async def renew_execution(
        self, tenant_id: str, invocation_id: str, claim: ExecutionClaim, ttl: timedelta
    ) -> bool: ...

    async def finalize_execution(
        self, tenant_id: str, invocation_id: str, claim: ExecutionClaim, result: ToolResult
    ) -> ToolInvocation:
        """C1: EXECUTING -> SUCCEEDED | FAILED | UNKNOWN, result_application_status=pending.
        Raises `ExecutionFencingError` if the epoch is stale."""
        ...

    async def claim_reconciliation(
        self, owner: str, limit: int, ttl: timedelta
    ) -> list[tuple[ToolInvocation, ExecutionClaim]]:
        """UNKNOWN / expired-EXECUTING / expired-RECONCILING -> RECONCILING (new epoch)."""
        ...

    async def finalize_reconciliation(
        self,
        tenant_id: str,
        invocation_id: str,
        claim: ExecutionClaim,
        result: ToolResult | None,
        *,
        handoff: bool = False,
    ) -> ToolInvocation:
        """RECONCILING -> RECONCILED (success) | FAILED | HUMAN_HANDOFF. `result=None` with
        handoff=False puts it back to UNKNOWN for a later attempt."""
        ...

    async def pending_application(
        self, tenant_id: str, conversation_id: str
    ) -> list[ToolInvocation]: ...
