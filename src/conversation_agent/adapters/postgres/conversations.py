from __future__ import annotations

from datetime import datetime

import asyncpg

from conversation_agent.core.errors import ConversationIdentityConflictError
from conversation_agent.core.models.conversation import ConversationIdentity


async def ensure_conversation(
    conn: asyncpg.Connection | asyncpg.Pool,
    identity: ConversationIdentity,
    now: datetime,
    scope: str | None = None,
) -> None:
    """Creates the conversation row on first contact (identity comes from the envelope,
    never from the LLM)."""
    await conn.execute(
        "INSERT INTO conversation_states (tenant_id, conversation_id, channel_id, contact_id, "
        "session_id, updated_at, scope) VALUES ($1,$2,$3,$4,$5,$6,$7) ON CONFLICT DO NOTHING",
        identity.tenant_id,
        identity.conversation_id,
        identity.channel_id,
        identity.contact_id,
        identity.session_id,
        now,
        scope,
    )
    row = await conn.fetchrow(
        "SELECT channel_id, contact_id, scope FROM conversation_states "
        "WHERE tenant_id = $1 AND conversation_id = $2",
        identity.tenant_id,
        identity.conversation_id,
    )
    if row is not None and (
        row["channel_id"] != identity.channel_id or row["contact_id"] != identity.contact_id
    ):
        raise ConversationIdentityConflictError(
            f"conversation_id {identity.conversation_id!r} already belongs to another "
            "channel/contact within this tenant"
        )
    if row is not None and scope is not None:
        if row["scope"] is None:  # from before scopes: the first scoped event adopts it
            await conn.execute(
                "UPDATE conversation_states SET scope = $3 WHERE tenant_id = $1 "
                "AND conversation_id = $2 AND scope IS NULL",
                identity.tenant_id,
                identity.conversation_id,
                scope,
            )
        elif row["scope"] != scope:
            raise ConversationIdentityConflictError(
                f"conversation_id {identity.conversation_id!r} is handled by another scope"
            )
