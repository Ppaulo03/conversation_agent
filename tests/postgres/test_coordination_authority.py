"""INV-032: coordination (leases, claims, backoff) is read from ONE authority - the database
clock - never from a worker's own wall clock."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from conversation_agent.adapters.clock import FixedClock
from conversation_agent.adapters.faults import NoFaults
from conversation_agent.adapters.postgres.coordination import (
    CoordinationTime,
    FixedCoordinationTime,
)
from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.adapters.postgres.lease import PostgresLeaseStore
from conversation_agent.core.errors import StaleWorkerError
from conversation_agent.engine.heartbeat import LeaseHandle
from conversation_agent.engine.reconciliation import ReconciliationWorker, reconcile_key
from postgres.world import KEY, World

NOW_FAR_BEHIND = datetime(2020, 1, 1, tzinfo=ZoneInfo("UTC"))
TTL = timedelta(seconds=30)


@pytest.fixture
def world(db: PostgresDatabase, clock: FixedClock) -> World:
    return World(db, clock)


async def test_stores_default_to_the_database_clock_not_the_workers_clock(
    db: PostgresDatabase, clock: FixedClock
) -> None:
    from postgres.world import event

    world = World(db, clock)
    await world.inbox.insert_if_absent(event("e1", clock=clock))
    wrong = FixedClock(NOW_FAR_BEHIND)  # a worker whose clock is years off
    leases = PostgresLeaseStore(db, wrong)  # NO coordination given: the default must be the DB
    lease = await leases.acquire(KEY, "w", TTL)
    assert lease is not None and lease.observed_at is not None
    real_now = await CoordinationTime(db).now()
    assert abs((lease.observed_at - real_now).total_seconds()) < 5  # database time...
    assert lease.expires_at - lease.observed_at == TTL  # ...for the grant and its expiry


async def test_a_workers_skewed_wall_clock_does_not_make_its_lease_look_expired(
    db: PostgresDatabase, clock: FixedClock
) -> None:
    from postgres.world import event

    world = World(db, clock)
    await world.inbox.insert_if_absent(event("e1", clock=clock))
    coordination = CoordinationTime(db)
    leases = PostgresLeaseStore(db, FixedClock(NOW_FAR_BEHIND))
    lease = await leases.acquire(KEY, "w", TTL)
    assert lease is not None
    handle = LeaseHandle(lease, leases, coordination, ttl=TTL, interval_seconds=10)
    handle.ensure_active()  # compared in the authority's domain: still active


async def test_a_lease_expires_by_elapsed_authority_time_not_by_wall_clock(
    db: PostgresDatabase, clock: FixedClock
) -> None:
    from postgres.world import event

    world = World(db, clock)
    await world.inbox.insert_if_absent(event("e1", clock=clock))
    leases = PostgresLeaseStore(db, clock)
    short = timedelta(seconds=1)
    lease = await leases.acquire(KEY, "w", short)
    assert lease is not None
    handle = LeaseHandle(lease, leases, CoordinationTime(db), ttl=short, interval_seconds=0.5)
    handle.ensure_active()
    await asyncio.sleep(1.2)  # real time passes; no heartbeat was started
    with pytest.raises(StaleWorkerError):
        handle.ensure_active()


async def test_reconciliation_backoff_is_a_coordination_timestamp() -> None:
    scheduled: list[Any] = []

    class Capture:
        async def schedule(self, event: Any) -> None:
            scheduled.append(event)

    authority = FixedClock(NOW_FAR_BEHIND)  # NOT the worker's application clock
    worker = ReconciliationWorker(
        ledger=SimpleNamespace(),  # type: ignore[arg-type]
        scheduler=Capture(),  # type: ignore[arg-type]
        faults=NoFaults(),
        coordination=FixedCoordinationTime(authority),
        owner="r",
        pipeline=SimpleNamespace(agent_id="x"),  # type: ignore[arg-type]
        retry_backoff=timedelta(seconds=45),
    )
    invocation = SimpleNamespace(tenant_id="t", invocation_id="inv1")
    await worker._schedule_retry(invocation)  # type: ignore[arg-type]
    assert scheduled[0].due_at == NOW_FAR_BEHIND + timedelta(seconds=45)
    assert scheduled[0].scheduler_key == reconcile_key("inv1")
