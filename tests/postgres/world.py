"""A small 'deployment' for reliability tests: PostgreSQL adapters + workers + fakes."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from conversation_agent.adapters.clock import FixedClock
from conversation_agent.adapters.faults import NoFaults
from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.adapters.postgres.inbox import PostgresInboxStore
from conversation_agent.adapters.postgres.lease import PostgresLeaseStore
from conversation_agent.adapters.postgres.outbox import PostgresOutboxStore
from conversation_agent.adapters.postgres.uow import PostgresTurnJournal, PostgresUnitOfWorkFactory
from conversation_agent.adapters.senders.fake import FakeMessageSender
from conversation_agent.adapters.tools.fake import FakeToolProvider
from conversation_agent.core.models.runtime import ConversationKey, FenceToken, InboundEvent
from conversation_agent.core.models.tooling import ToolResult
from conversation_agent.engine.outbox_worker import OutboxWorker
from conversation_agent.engine.turn_coordinator import TurnCoordinator
from conversation_agent.engine.turn_engine import TurnEngine
from conversation_agent.ports.faults import FaultInjector
from conversation_agent.ports.journal import TurnJournal
from conversation_agent.ports.llm import LLMProvider
from support.builders import IDENTITY
from vertical_slice.wiring import build_engine

KEY = ConversationKey(tenant_id=IDENTITY.tenant_id, conversation_id=IDENTITY.conversation_id)
SLOTS = ToolResult(status="success", data={"items": [], "pagination": {"next_cursor": None}})
TTL = timedelta(seconds=30)


def event(
    event_id: str,
    text: str = "oi",
    *,
    clock: FixedClock,
    occurred_offset_s: float = 0,
    sequence: int | None = None,
) -> InboundEvent:
    at = clock.now() + timedelta(seconds=occurred_offset_s)
    return InboundEvent(
        tenant_id=IDENTITY.tenant_id,
        channel_id=IDENTITY.channel_id,
        event_id=event_id,
        conversation_id=IDENTITY.conversation_id,
        contact_id=IDENTITY.contact_id,
        session_id=IDENTITY.session_id,
        text=text,
        occurred_at=at,
        received_at=at,
        source_sequence=sequence,
    )


class World:
    """Shared stores (one database); `coordinator(owner, llm)` makes a worker process."""

    def __init__(self, db: PostgresDatabase, clock: FixedClock) -> None:
        self.db = db
        self.clock = clock
        self.inbox = PostgresInboxStore(db, clock)
        self.leases = PostgresLeaseStore(db, clock)
        self.uows = PostgresUnitOfWorkFactory(db, clock)
        self.outbox = PostgresOutboxStore(db, clock)
        self.sender = FakeMessageSender()
        self.tools = FakeToolProvider({"erp_get_available_slots": SLOTS})

    def coordinator(
        self,
        owner: str,
        llm: LLMProvider,
        *,
        faults: FaultInjector | None = None,
        heartbeat_interval_seconds: float = 10.0,
        **kwargs: Any,
    ) -> TurnCoordinator:
        def journal_factory(fence: FenceToken) -> TurnJournal:
            return PostgresTurnJournal(self.uows, self.db, fence)

        def engine_factory(fence: FenceToken, journal: TurnJournal) -> TurnEngine:
            engine, _, _ = build_engine(
                llm,
                api_base_url="http://unused",
                journal=journal,
                clock=self.clock,
                providers={"http": self.tools},
            )
            return engine

        return TurnCoordinator(
            owner=owner,
            leases=self.leases,
            uows=self.uows,
            inbox=self.inbox,
            journal_factory=journal_factory,
            engine_factory=engine_factory,
            clock=self.clock,
            faults=faults or NoFaults(),
            lease_ttl=TTL,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
            **kwargs,
        )

    def outbox_worker(self, owner: str, faults: FaultInjector | None = None) -> OutboxWorker:
        return OutboxWorker(
            self.outbox, self.sender, faults or NoFaults(), owner=owner, claim_ttl=TTL
        )

    async def count(self, table: str, where: str = "true") -> int:
        value = await self.db.pool.fetchval(f"SELECT count(*) FROM {table} WHERE {where}")
        return int(value)
