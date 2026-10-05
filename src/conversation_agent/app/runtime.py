"""The durable runtime, assembled once: PostgreSQL stores + the workers that drive them.

`Runtime.build(...)` is the composition root every deployment (and the `serve` command) uses: the
turn coordinator with the ledger executor and confirmation stage, the outbox worker, the timers
(proactive messages, reconciliation wake-ups) and the invocation reconciler. What varies per
deployment is passed in: the database, the compiled agent, the LLM, the tool providers and the
channel's sender. `tick()` does one round of work; `run()` repeats it until told to stop.

Coordination time (leases, claims, backoff) is the database's clock; the application clock only
carries conversational time (INV-032).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import timedelta
from typing import Any

from conversation_agent.adapters.clock import SystemClock
from conversation_agent.adapters.faults import NoFaults
from conversation_agent.adapters.postgres.coordination import CoordinationTime
from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.adapters.postgres.inbox import PostgresInboxStore
from conversation_agent.adapters.postgres.lease import PostgresLeaseStore
from conversation_agent.adapters.postgres.ledger import PostgresToolInvocationStore
from conversation_agent.adapters.postgres.outbox import PostgresOutboxStore
from conversation_agent.adapters.postgres.scheduler import PostgresScheduler
from conversation_agent.adapters.postgres.uow import PostgresTurnJournal, PostgresUnitOfWorkFactory
from conversation_agent.core.compiler import CompiledAgent
from conversation_agent.core.models.delivery import DeliveryPolicy
from conversation_agent.core.models.runtime import FenceToken, InboundEvent
from conversation_agent.engine.capability_pipeline import CapabilityPipeline
from conversation_agent.engine.confirmation_stage import ConfirmationStage
from conversation_agent.engine.outbox_reconciler import OutboxReconciler
from conversation_agent.engine.outbox_worker import OutboxWorker
from conversation_agent.engine.policy_gate import PolicyGate
from conversation_agent.engine.proactive import PROACTIVE_EVENT, ProactiveEventHandler
from conversation_agent.engine.reconciliation import RECONCILE_EVENT, ReconciliationWorker
from conversation_agent.engine.scheduler_worker import SchedulerWorker
from conversation_agent.engine.side_effects import LedgerToolExecutor
from conversation_agent.engine.tool_runner import ToolRunner
from conversation_agent.engine.turn_coordinator import TurnCoordinator
from conversation_agent.engine.turn_engine import TurnEngine
from conversation_agent.ports.clock import Clock
from conversation_agent.ports.journal import TurnJournal
from conversation_agent.ports.llm import LLMProvider
from conversation_agent.ports.sender import MessageSender
from conversation_agent.ports.tool_provider import ToolProvider
from conversation_agent.ports.transcriber import Transcriber

log = logging.getLogger("conversation_agent.runtime")

DEFAULT_LEASE_TTL = timedelta(seconds=30)


class Runtime:
    def __init__(
        self,
        *,
        coordinator: TurnCoordinator,
        outbox_worker: OutboxWorker,
        scheduler_worker: SchedulerWorker,
        reconciler: ReconciliationWorker,
        inbox: PostgresInboxStore,
        owner: str,
        scope: str | None = None,
        outbox_reconciler: OutboxReconciler | None = None,
    ) -> None:
        self.scope = scope
        self.outbox_reconciler = outbox_reconciler
        self.coordinator = coordinator
        self.outbox_worker = outbox_worker
        self.scheduler_worker = scheduler_worker
        self.reconciler = reconciler
        self.inbox = inbox
        self.owner = owner

    @classmethod
    def build(
        cls,
        *,
        db: PostgresDatabase,
        compiled: CompiledAgent,
        llm: LLMProvider,
        providers: Mapping[str, ToolProvider],
        sender: MessageSender,
        clock: Clock | None = None,
        owner: str | None = None,
        scope: str | None = None,
        policy: PolicyGate | None = None,
        transcriber: Transcriber | None = None,
        outbox_retry_horizon: timedelta | None = None,
        delivery_policy: DeliveryPolicy | None = None,
        outbox_poll_after: timedelta = timedelta(seconds=30),
        lease_ttl: timedelta = DEFAULT_LEASE_TTL,
        **coordinator_options: Any,
    ) -> Runtime:
        """Everything a deployment needs, wired the way the reliability tests prove correct.
        `scope` says which conversations this runtime handles (default: the agent's id): several
        runtimes can share one database, each claiming only the turns and the outbox messages of
        its own conversations. With a `delivery_policy` (how long the channel remembers an
        idempotency key) and a sender that can `lookup(message)`, sends whose outcome the channel
        did not settle (queued, unknown) are reconciled instead of left hanging, and the policy's
        retry horizon bounds re-sending. `lease_ttl` is how long a conversation lease and every
        worker claim lasts without renewal: a worker that dies is recovered after that long (a turn
        renews its lease every `heartbeat_interval_seconds`, default a third of the TTL up to 10 s,
        and the TTL must be more than twice the heartbeat). `coordinator_options` go to the
        TurnCoordinator.
        """
        scope = scope or compiled.agent_id
        heartbeat = float(
            coordinator_options.pop(
                "heartbeat_interval_seconds", min(10.0, lease_ttl.total_seconds() / 3)
            )
        )
        if heartbeat <= 0 or lease_ttl.total_seconds() <= 2 * heartbeat:
            raise ValueError(
                "lease_ttl must be more than twice heartbeat_interval_seconds, or a lease could "
                "expire in the middle of a turn that is still running"
            )
        if delivery_policy is not None and not hasattr(sender, "lookup"):
            raise ValueError("a delivery_policy needs a sender that can look a message up")
        if delivery_policy is not None and outbox_retry_horizon is None:
            outbox_retry_horizon = delivery_policy.retry_horizon
        agent = compiled.agent
        clock = clock or SystemClock(agent.timezone)
        owner = owner or f"worker-{uuid.uuid4().hex[:8]}"
        coordination = CoordinationTime(db)
        faults = NoFaults()
        inbox = PostgresInboxStore(db, clock)
        leases = PostgresLeaseStore(db, clock, coordination)
        uows = PostgresUnitOfWorkFactory(db, clock, coordination)
        ledger = PostgresToolInvocationStore(db, clock, coordination)
        outbox = PostgresOutboxStore(db, clock, coordination=coordination)
        scheduler = PostgresScheduler(db, clock, coordination)
        pipeline = CapabilityPipeline(
            compiled, policy or PolicyGate(agent.allowed_capabilities), ToolRunner(providers)
        )

        def journal_factory(fence: FenceToken) -> TurnJournal:
            return PostgresTurnJournal(uows, db, fence)

        def engine_factory(fence: FenceToken, journal: TurnJournal) -> TurnEngine:
            executor = LedgerToolExecutor(
                pipeline=pipeline,
                uows=uows,
                ledger=ledger,
                fence=fence,
                faults=faults,
                clock=clock,
                owner=owner,
            )
            engine = TurnEngine(
                compiled,
                llm,
                pipeline,
                journal,
                clock,
                tool_executor=executor,
                transcriber=transcriber,
            )
            engine.attach_confirmation(
                ConfirmationStage(
                    agent=compiled,
                    pipeline=pipeline,
                    uows=uows,
                    executor=executor,
                    fence=fence,
                    faults=faults,
                    host=engine,
                    skew_tolerance=timedelta(seconds=2),
                )
            )
            return engine

        async def wake(_: Any) -> None:  # a due timer lifts the backoff of the invocation
            return None

        return cls(
            coordinator=TurnCoordinator(
                owner=owner,
                leases=leases,
                uows=uows,
                inbox=inbox,
                journal_factory=journal_factory,
                engine_factory=engine_factory,
                clock=clock,
                coordination=coordination,
                faults=faults,
                lease_ttl=lease_ttl,
                heartbeat_interval_seconds=heartbeat,
                scope=scope,
                **coordinator_options,
            ),
            outbox_worker=OutboxWorker(
                outbox,
                sender,
                faults,
                owner=owner,
                claim_ttl=lease_ttl,
                retry_horizon=outbox_retry_horizon,
                coordination=coordination,
                scope=scope,
            ),
            scheduler_worker=SchedulerWorker(
                scheduler,
                {RECONCILE_EVENT: wake, PROACTIVE_EVENT: ProactiveEventHandler(inbox, clock)},
                owner=owner,
                claim_ttl=lease_ttl,
            ),
            reconciler=ReconciliationWorker(
                ledger=ledger,
                pipeline=pipeline,
                scheduler=scheduler,
                faults=faults,
                coordination=coordination,
                owner=owner,
            ),
            inbox=inbox,
            owner=owner,
            scope=scope,
            outbox_reconciler=(
                OutboxReconciler(
                    outbox,
                    sender,  # type: ignore[arg-type]  # checked above: it has `lookup`
                    coordination,
                    delivery_policy,
                    owner=owner,
                    claim_ttl=lease_ttl,
                    poll_after=outbox_poll_after,
                    scope=scope,
                )
                if delivery_policy is not None
                else None
            ),
        )

    async def receive(self, event: InboundEvent) -> bool:
        """Persist an inbound event (the conversation is created on first contact, in this
        runtime's scope unless the event names one). False when it was already there (a duplicate
        delivery)."""
        if event.scope is None and self.scope is not None:
            event = event.model_copy(update={"scope": self.scope})
        return await self.inbox.insert_if_absent(event)

    async def tick(self) -> int:
        """One round: turns, then deliveries, then timers and reconciliation. A stage that fails
        is logged and the others still run; the work it left is retried on the next round."""
        done = 0
        stages: list[Callable[[], Awaitable[object]]] = [
            self.coordinator.run_once,
            self.outbox_worker.run_once,
            self.scheduler_worker.run_once,
            self.reconciler.run_once,
        ]
        if self.outbox_reconciler is not None:
            stages.append(self.outbox_reconciler.run_once)
        for stage in stages:
            try:
                result = await stage()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("runtime stage failed: %s", getattr(stage, "__qualname__", stage))
                continue
            done += result if isinstance(result, int) else len(result)  # type: ignore[arg-type]
        return done

    async def drain(self, *, quiet_rounds: int = 2) -> None:
        """Run rounds until `quiet_rounds` in a row find nothing to do (what a script or a piped
        session needs before it exits: the replies were produced AND delivered)."""
        quiet = 0
        while quiet < quiet_rounds:
            quiet = quiet + 1 if await self.tick() == 0 else 0

    async def run(self, stop: asyncio.Event, *, poll_interval: float = 0.5) -> None:
        """Poll until `stop` is set; sleeps only when a round found nothing to do."""
        while not stop.is_set():
            if await self.tick() == 0:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=poll_interval)
