"""Ownership transitions (DESIGN 29): who is allowed to talk in a conversation.

    BOT --request--> HANDOFF_PENDING --assign--> HUMAN --return--> BOT
    BOT --take over (a person writes first)--> HUMAN

Every change is made under the CONVERSATION LEASE and a fenced unit of work, like any other write
to the conversation: it can never interleave with a turn in flight, and a stale worker cannot do
it. While the conversation is not BOT's, inbound events are stored and nothing else happens (no
Router, no LLM, no tool, no outbound, INV-019); timers keep firing but stay silent.
"""

from __future__ import annotations

from datetime import timedelta

from conversation_agent.core.errors import ConversationAgentError
from conversation_agent.core.models.runtime import ConversationKey, Ownership
from conversation_agent.ports.lease import ConversationLeaseStore
from conversation_agent.ports.uow import ConversationUnitOfWorkFactory

_ALLOWED: dict[Ownership, frozenset[Ownership]] = {
    Ownership.BOT: frozenset({Ownership.HANDOFF_PENDING, Ownership.HUMAN}),
    Ownership.HANDOFF_PENDING: frozenset({Ownership.HUMAN, Ownership.BOT}),
    Ownership.HUMAN: frozenset({Ownership.BOT}),
}


class OwnershipTransitionError(ConversationAgentError):
    """The requested change is not a valid ownership transition."""


class ConversationBusyError(ConversationAgentError):
    """A turn holds the conversation right now: retry the change in a moment."""


class OwnershipService:
    def __init__(
        self,
        leases: ConversationLeaseStore,
        uows: ConversationUnitOfWorkFactory,
        *,
        owner: str,
        ttl: timedelta = timedelta(seconds=15),
    ) -> None:
        self._leases = leases
        self._uows = uows
        self._owner = owner
        self._ttl = ttl

    async def request_handoff(self, key: ConversationKey) -> Ownership:
        """The bot (or an operator rule) asks for a person: BOT -> HANDOFF_PENDING."""
        return await self._move(key, Ownership.HANDOFF_PENDING)

    async def assign_human(self, key: ConversationKey) -> Ownership:
        """A person takes the conversation (from BOT directly, or from a pending handoff)."""
        return await self._move(key, Ownership.HUMAN)

    async def return_to_bot(self, key: ConversationKey) -> Ownership:
        return await self._move(key, Ownership.BOT)

    async def _move(self, key: ConversationKey, target: Ownership) -> Ownership:
        lease = await self._leases.acquire(key, self._owner, self._ttl)
        if lease is None:
            raise ConversationBusyError(f"{key.conversation_id} is being processed")
        try:
            async with self._uows.begin(lease.fence) as uow:
                current = (await uow.state.load()).ownership
                if current is target:
                    return current  # idempotent: already there
                if target not in _ALLOWED[current]:
                    raise OwnershipTransitionError(
                        f"{current.value} -> {target.value} is not allowed"
                    )
                await uow.state.set_ownership(target)
                await uow.commit()
                return target
        finally:
            await self._leases.release(lease)
