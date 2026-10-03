"""Row <-> model mapping shared by the PostgreSQL adapters."""

from __future__ import annotations

from typing import Any

import asyncpg

from conversation_agent.core.models.runtime import (
    ApplicationStatus,
    ExecutionIntent,
    InvocationStatus,
    OutboundMessage,
    OutboxStatus,
    ToolInvocation,
)
from conversation_agent.core.models.tooling import CapabilityRequest, ToolContext

INVOCATION_COLUMNS = (
    "tenant_id, invocation_id, conversation_id, session_id, turn_id, logical_step_id, "
    "attempt_semantic_id, action_id, tool_name, capability, args_hash, idempotency_key, "
    "request_json, context_json, status, execution_owner, execution_lease_expires_at, "
    "execution_epoch, result_application_status, result_json, error_json, provider_metadata, "
    "reconcile_attempts, intent_json"
)

OUTBOX_COLUMNS = (
    "tenant_id, outbox_id, conversation_id, channel_id, contact_id, turn_id, message_index, "
    "action_id, text, idempotency_key, status, attempts, provider_message_id"
)


def invocation_from_row(r: asyncpg.Record | dict[str, Any]) -> ToolInvocation:
    return ToolInvocation(
        tenant_id=r["tenant_id"],
        invocation_id=r["invocation_id"],
        conversation_id=r["conversation_id"],
        session_id=r["session_id"],
        turn_id=r["turn_id"],
        logical_step_id=r["logical_step_id"],
        attempt_semantic_id=r["attempt_semantic_id"],
        action_id=r["action_id"],
        tool_name=r["tool_name"],
        capability=r["capability"],
        args_hash=r["args_hash"],
        idempotency_key=r["idempotency_key"],
        request=CapabilityRequest.model_validate(r["request_json"]),
        context=ToolContext.model_validate(r["context_json"]),
        intent=ExecutionIntent.model_validate(r["intent_json"]),
        status=InvocationStatus(r["status"]),
        execution_owner=r["execution_owner"],
        execution_lease_expires_at=r["execution_lease_expires_at"],
        execution_epoch=r["execution_epoch"],
        reconcile_attempts=r["reconcile_attempts"],
        result_application_status=ApplicationStatus(r["result_application_status"]),
        result=r["result_json"],
        error=r["error_json"],
        provider_metadata=r["provider_metadata"] or {},
    )


def outbox_from_row(r: asyncpg.Record | dict[str, Any]) -> OutboundMessage:
    return OutboundMessage(
        outbox_id=r["outbox_id"],
        tenant_id=r["tenant_id"],
        conversation_id=r["conversation_id"],
        channel_id=r["channel_id"],
        contact_id=r["contact_id"],
        turn_id=r["turn_id"],
        message_index=r["message_index"],
        action_id=r["action_id"],
        text=r["text"],
        idempotency_key=r["idempotency_key"],
        status=OutboxStatus(r["status"]),
        attempts=r["attempts"],
        provider_message_id=r["provider_message_id"],
    )
