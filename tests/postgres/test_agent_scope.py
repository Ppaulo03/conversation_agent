"""Phase 14 (POC item 4): runtimes sharing one database do not take each other's work.

Each runtime claims only the turns, the outbox messages and the reconciliation of conversations of
ITS scope (by default its agent's id). A conversation's scope is fixed at first contact.
"""

from __future__ import annotations

import pytest

from conftest import ApiHandle
from conversation_agent.adapters.clock import SystemClock
from conversation_agent.adapters.llm.fake import FakeLLM, text_response
from conversation_agent.adapters.postgres.conversations import ensure_conversation
from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.adapters.senders.console import ConsoleChannel
from conversation_agent.adapters.tools.http import HTTPToolProvider, local_dev_connection
from conversation_agent.app.runtime import Runtime
from conversation_agent.core.errors import ConversationIdentityConflictError
from conversation_agent.core.models.conversation import ConversationIdentity
from conversation_agent.ports.clock import Clock
from vertical_slice.definitions import CONNECTION
from vertical_slice.wiring import load_compiled_agent


@pytest.fixture
def clock() -> Clock:
    return SystemClock("America/Sao_Paulo")


def identity(name: str) -> ConversationIdentity:
    return ConversationIdentity(
        tenant_id="tenant-1",
        channel_id=f"channel-{name}",
        conversation_id=f"conv-{name}",
        session_id=f"sess-{name}",
        contact_id=f"contact-{name}",
    )


def deployment(
    db: PostgresDatabase, clock: Clock, api: ApiHandle, name: str, reply: str
) -> tuple[Runtime, ConsoleChannel, list[str]]:
    out: list[str] = []
    channel = ConsoleChannel(identity(name), clock, write=out.append)
    runtime = Runtime.build(
        db=db,
        compiled=load_compiled_agent(),
        llm=FakeLLM([text_response(reply)]),
        providers={
            "http": HTTPToolProvider.static({CONNECTION: local_dev_connection(api.base_url)})
        },
        sender=channel,
        clock=clock,
        scope=name,
    )
    return runtime, channel, out


async def test_a_runtime_does_not_take_the_turns_of_another_scope(
    db: PostgresDatabase, clock: Clock, api: ApiHandle
) -> None:
    quadras, qa, out_quadras = deployment(db, clock, api, "quadras", "Olá, aqui são as quadras!")
    clinica, qb, out_clinica = deployment(db, clock, api, "clinica", "Olá, aqui é a clínica!")
    await quadras.receive(qa.inbound("oi"))
    await clinica.receive(qb.inbound("oi"))

    await clinica.drain()  # the other deployment's event is right there in the same database
    assert out_clinica == ["bot> Olá, aqui é a clínica!"] and out_quadras == []
    pending = await db.pool.fetchval(
        "SELECT count(*) FROM inbox_events WHERE conversation_id = 'conv-quadras' "
        "AND status = 'READY'"
    )
    assert pending == 1  # untouched

    await quadras.drain()
    assert out_quadras == ["bot> Olá, aqui são as quadras!"]


async def test_a_runtime_does_not_send_the_outbox_messages_of_another_scope(
    db: PostgresDatabase, clock: Clock, api: ApiHandle
) -> None:
    quadras, qa, out_quadras = deployment(db, clock, api, "quadras", "Olá!")
    clinica, _, out_clinica = deployment(db, clock, api, "clinica", "x")
    await quadras.receive(qa.inbound("oi"))
    await quadras.coordinator.run_once()  # the reply is in the outbox, not yet sent

    assert await clinica.outbox_worker.run_once() == 0 and out_clinica == []  # not its message
    assert await quadras.outbox_worker.run_once() == 1 and out_quadras == ["bot> Olá!"]


async def test_the_scope_defaults_to_the_agent_and_is_stamped_on_first_contact(
    db: PostgresDatabase, clock: Clock, api: ApiHandle
) -> None:
    out: list[str] = []
    channel = ConsoleChannel(identity("x"), clock, write=out.append)
    runtime = Runtime.build(
        db=db,
        compiled=load_compiled_agent(),
        llm=FakeLLM([]),
        providers={
            "http": HTTPToolProvider.static({CONNECTION: local_dev_connection(api.base_url)})
        },
        sender=channel,
        clock=clock,
    )
    assert runtime.scope == load_compiled_agent().agent_id
    await runtime.receive(channel.inbound("oi"))
    stored = await db.pool.fetchval("SELECT scope FROM conversation_states")
    assert stored == runtime.scope


async def test_a_conversation_never_changes_scope(
    db: PostgresDatabase, clock: Clock, api: ApiHandle
) -> None:
    quadras, qa, _ = deployment(db, clock, api, "quadras", "x")
    clinica, _, _ = deployment(db, clock, api, "clinica", "x")
    await quadras.receive(qa.inbound("oi"))
    stolen = qa.inbound("outra").model_copy(update={"scope": "clinica"})
    with pytest.raises(ConversationIdentityConflictError):
        await clinica.receive(stolen)  # same conversation, another scope


async def test_a_conversation_from_before_scopes_is_adopted_by_the_first_scoped_event(
    db: PostgresDatabase, clock: Clock, api: ApiHandle
) -> None:
    await ensure_conversation(db.pool, identity("quadras"), clock.now())  # legacy: scope NULL
    quadras, qa, out = deployment(db, clock, api, "quadras", "Bem-vindo de volta!")
    await quadras.receive(qa.inbound("oi"))
    assert await db.pool.fetchval("SELECT scope FROM conversation_states") == "quadras"
    await quadras.drain()
    assert out == ["bot> Bem-vindo de volta!"]


async def test_an_unscoped_worker_still_sees_everything(
    db: PostgresDatabase, clock: Clock, api: ApiHandle
) -> None:  # tests and single-purpose scripts that build the coordinator by hand
    quadras, qa, _ = deployment(db, clock, api, "quadras", "x")
    await quadras.receive(qa.inbound("oi"))
    assert len(await quadras.inbox.list_ready_conversations(10)) == 1  # no scope: all
    assert await quadras.inbox.list_ready_conversations(10, "clinica") == []
    assert len(await quadras.inbox.list_ready_conversations(10, "quadras")) == 1
