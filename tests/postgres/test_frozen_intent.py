"""INV-023: a PREPARED invocation is immutable as to the external operation it represents.

Retry and reconciliation execute the operation frozen at PREPARE (concrete tool args, provider,
connection, endpoint, recovery contract). They never re-map or re-resolve it using definitions
deployed later; if the resolved operation changed they refuse and escalate.
Also: provider_metadata reaches the ledger, absent_codes gate "it did not happen", reads recover
safely by default, reconciliation is scoped by agent, and conversation identity is explicit.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest

from conftest import ApiHandle
from conversation_agent.adapters.clock import FixedClock
from conversation_agent.adapters.faults import ChaosFaults, SimulatedCrash
from conversation_agent.adapters.llm.fake import FakeLLM, text_response, tool_call_response
from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.core.definitions.agent import AgentDefinition
from conversation_agent.core.definitions.tool import RecoverySpec
from conversation_agent.core.errors import ConversationIdentityConflictError
from conversation_agent.core.models.runtime import InvocationStatus, ToolInvocation
from conversation_agent.engine.capability_pipeline import CapabilityPipeline
from conversation_agent.engine.tool_runner import ToolRunner
from conversation_agent.ports.llm import LLMRequest
from postgres.world import KEY, TTL, AllowWrites, World, event
from support.builders import IDENTITY, availability_call
from vertical_slice.definitions import (
    CREATE_BINDING,
    ERP_CREATE_RESERVATION,
    build_agent,
)

BOOKING_ARGS = {
    "service_id": "haircut",
    "start_at": "2026-10-06T10:00:00-03:00",
    "duration_minutes": 30,
}
ALLOW = frozenset({"scheduling.availability", "scheduling.create"})


@pytest.fixture
def world(db: PostgresDatabase, clock: FixedClock) -> World:
    return World(db, clock)


def posts(api: ApiHandle) -> list[dict[str, object]]:
    return [r for r in api.requests if r["method"] == "POST"]


def book_first() -> FakeLLM:
    return FakeLLM([tool_call_response("scheduling__create", BOOKING_ARGS)])


def pipeline_for(world: World, api: ApiHandle, agent: AgentDefinition) -> CapabilityPipeline:
    return CapabilityPipeline(
        agent, AllowWrites(ALLOW), ToolRunner({"http": world.http_provider(api.base_url)})
    )


def changed_agent(*, tool: Any = None, binding: Any = None) -> AgentDefinition:
    agent = build_agent()
    tools = tuple(tool if tool and t.name == tool.name else t for t in agent.tools)
    bindings = tuple(
        binding if binding and b.capability == binding.capability else b for b in agent.bindings
    )
    return agent.model_copy(update={"tools": tools, "bindings": bindings})


async def prepare_then_crash_before_request(world: World, api: ApiHandle) -> ToolInvocation:
    await world.inbox.insert_if_absent(event("e1", "terça 10h", clock=world.clock))
    pipeline = pipeline_for(world, api, build_agent())
    with pytest.raises(SimulatedCrash):
        await world.coordinator(
            "w1",
            book_first(),
            pipeline=pipeline,
            faults=ChaosFaults("C04_before_external_request"),
        ).process_conversation(KEY)
    row = await world.db.pool.fetchval("SELECT invocation_id FROM tool_invocations")
    inv = await world.ledger.get(KEY.tenant_id, row)
    assert inv is not None
    world.clock.set(world.clock.now() + timedelta(seconds=61))  # leases expire
    return inv


# --- INV-023 ---


async def test_prepared_invocation_executes_the_frozen_intent_never_a_re_resolved_one(
    world: World, api: ApiHandle
) -> None:
    inv = await prepare_then_crash_before_request(world, api)
    assert inv.intent.tool_args == {  # the concrete operation, frozen at PREPARE
        "service_code": "HC-01",
        "starts_at": "2026-10-06T13:00:00+00:00",
        "hours": 0.5,
    }
    assert inv.intent.recovery.strategy == "status_lookup"
    assert inv.intent.recovery.absent_codes == ("BOOKING_NOT_FOUND",)

    # A new build now points `scheduling.create` at a different endpoint...
    moved_http = ERP_CREATE_RESERVATION.http.model_copy(update={"path": "/v2/bookings"})  # type: ignore[union-attr]
    other_endpoint = ERP_CREATE_RESERVATION.model_copy(update={"http": moved_http})
    pipeline = pipeline_for(world, api, changed_agent(tool=other_endpoint))
    (final,) = await world.reconciler(
        "r", providers={"http": world.http_provider(api.base_url)}, pipeline=pipeline
    ).run_once()

    assert final.status is InvocationStatus.HUMAN_HANDOFF  # ...so it must not guess
    assert final.result is not None and final.result["error"]["code"] == "INTENT_CHANGED"
    assert posts(api) == []  # nothing was sent to the old endpoint NOR to the new one
    assert not [r for r in api.requests if str(r["path"]).startswith("/v2")]


async def test_a_changed_input_mapping_also_refuses_to_execute(
    world: World, api: ApiHandle
) -> None:
    await prepare_then_crash_before_request(world, api)
    remapped = CREATE_BINDING.model_copy(
        update={"input_map": {**CREATE_BINDING.input_map, "hours": "$.duration_minutes"}}
    )
    pipeline = pipeline_for(world, api, changed_agent(binding=remapped))
    (final,) = await world.reconciler(
        "r", providers={"http": world.http_provider(api.base_url)}, pipeline=pipeline
    ).run_once()
    assert final.status is InvocationStatus.HUMAN_HANDOFF
    assert posts(api) == []


async def test_recovery_follows_the_frozen_contract_not_todays_configuration(
    world: World, api: ApiHandle
) -> None:
    await prepare_then_crash_before_request(world, api)
    # Today's definition says "human_handoff", but the operation was prepared under status_lookup.
    # Recovery-contract/output changes do not move the operation, so execution is still allowed.
    stricter = ERP_CREATE_RESERVATION.model_copy(
        update={"recovery": RecoverySpec(strategy="human_handoff")}
    )
    pipeline = pipeline_for(world, api, changed_agent(tool=stricter))
    (final,) = await world.reconciler(
        "r", providers={"http": world.http_provider(api.base_url)}, pipeline=pipeline
    ).run_once()
    assert final.status is InvocationStatus.RECONCILED
    assert len(posts(api)) == 1 and len(api.state.bookings) == 1
    assert posts(api)[0]["headers"]["idempotency-key"] == final.invocation_id  # type: ignore[index]


async def test_run_frozen_sends_exactly_the_frozen_tool_args(world: World) -> None:
    pipeline = world.pipeline()
    from conversation_agent.tools.requests import build_capability_request

    resolved = pipeline.resolve("scheduling.availability")
    request = build_capability_request(
        resolved.capability,
        {"service_id": "haircut", "from_date": "2026-10-06", "to_date": "2026-10-06"},
    )
    intent = pipeline.freeze(request)
    assert not hasattr(intent, "status")  # a frozen intent, not a failure
    tampered = intent.model_copy(update={"tool_args": {**intent.tool_args, "limit": 7}})  # type: ignore[union-attr]
    await pipeline.run_frozen(tampered, _ctx())  # type: ignore[arg-type]
    assert world.tools.calls[0].args["limit"] == 7  # nothing is re-mapped on the way


async def test_a_deploy_between_prepare_and_execute_fails_closed_without_sending(
    world: World, api: ApiHandle
) -> None:
    class Drifted(CapabilityPipeline):
        def intent_matches(self, intent: Any) -> bool:
            return False  # the deployed definitions no longer match what was prepared

    pipeline = Drifted(
        build_agent(), AllowWrites(ALLOW), ToolRunner({"http": world.http_provider(api.base_url)})
    )
    await world.inbox.insert_if_absent(event("e1", "terça 10h", clock=world.clock))
    seen: dict[str, Any] = {}

    def observe(request: LLMRequest) -> Any:
        parts = [p for m in request.messages for p in m.parts if hasattr(p, "tool_call_id")]
        seen.update(json.loads(parts[-1].content))  # type: ignore[attr-defined]
        return text_response("Não consegui agendar.")

    llm = FakeLLM([tool_call_response("scheduling__create", BOOKING_ARGS), observe])
    await world.coordinator("w1", llm, pipeline=pipeline).process_conversation(KEY)

    inv = await world.ledger.get(
        KEY.tenant_id, await world.db.pool.fetchval("SELECT invocation_id FROM tool_invocations")
    )
    assert inv is not None and inv.status is InvocationStatus.FAILED  # known non-execution
    assert posts(api) == [] and seen["error"]["code"] == "INTENT_CHANGED"


# --- absent_codes: only the declared "not found" proves absence ---


async def test_only_declared_absent_codes_authorise_a_resend(world: World, api: ApiHandle) -> None:
    picky = ERP_CREATE_RESERVATION.model_copy(
        update={
            "recovery": RecoverySpec(
                strategy="status_lookup",
                lookup_capability="scheduling.lookup_booking",
                absent_codes=("SOMETHING_ELSE",),  # the lookup's real code is BOOKING_NOT_FOUND
            )
        }
    )
    pipeline = pipeline_for(world, api, changed_agent(tool=picky))
    await world.inbox.insert_if_absent(event("e1", clock=world.clock))
    with pytest.raises(SimulatedCrash):
        await world.coordinator(
            "w1",
            book_first(),
            pipeline=pipeline,
            faults=ChaosFaults("C04_before_external_request"),
        ).process_conversation(KEY)
    world.clock.set(world.clock.now() + timedelta(seconds=61))

    (final,) = await world.reconciler(
        "r", providers={"http": world.http_provider(api.base_url)}, pipeline=pipeline
    ).run_once()
    assert final.status is InvocationStatus.UNKNOWN  # an error that is not proof of absence
    assert posts(api) == []  # so nothing is re-sent


# --- provider_metadata reaches the ledger, never the LLM ---


async def test_provider_metadata_is_recorded_in_the_ledger_but_never_shown_to_the_llm(
    world: World, api: ApiHandle
) -> None:
    await world.inbox.insert_if_absent(event("e1", clock=world.clock))
    shown: list[str] = []

    def observe(request: LLMRequest) -> Any:
        parts = [p for m in request.messages for p in m.parts if hasattr(p, "tool_call_id")]
        shown.append(parts[-1].content)  # type: ignore[attr-defined]
        return text_response("Agendado!")

    llm = FakeLLM([tool_call_response("scheduling__create", BOOKING_ARGS), observe])
    await world.coordinator(
        "w1", llm, pipeline=pipeline_for(world, api, build_agent())
    ).process_conversation(KEY)

    inv = await world.ledger.get(
        KEY.tenant_id, await world.db.pool.fetchval("SELECT invocation_id FROM tool_invocations")
    )
    assert inv is not None
    assert inv.provider_metadata["http_status"] == 201 and "duration_ms" in inv.provider_metadata
    assert inv.result is not None and inv.result["provider_metadata"]["http_status"] == 201
    assert "http_status" not in shown[0] and "duration_ms" not in shown[0]


# --- reads recover safely by default ---


async def test_an_abandoned_read_is_safely_re_run_not_sent_to_a_human(
    world: World, api: ApiHandle
) -> None:
    await world.inbox.insert_if_absent(event("e1", "terça?", clock=world.clock))
    pipeline = pipeline_for(world, api, build_agent())
    with pytest.raises(SimulatedCrash):
        await world.coordinator(
            "w1",
            FakeLLM([availability_call("haircut", "2026-10-06")]),
            pipeline=pipeline,
            faults=ChaosFaults("C04_before_external_request"),
        ).process_conversation(KEY)
    world.clock.set(world.clock.now() + timedelta(seconds=61))
    assert api.availability_requests() == []  # the read never left

    (final,) = await world.reconciler(
        "r", providers={"http": world.http_provider(api.base_url)}, pipeline=pipeline
    ).run_once()
    assert final.intent.recovery.strategy == "safe_retry"  # default for a read with no contract
    assert final.status is InvocationStatus.RECONCILED
    assert len(api.availability_requests()) == 1


# --- reconciliation is scoped by agent ---


async def test_reconciliation_only_claims_invocations_of_the_agent_it_serves(
    world: World, api: ApiHandle
) -> None:
    await prepare_then_crash_before_request(world, api)
    assert await world.ledger.claim_reconciliation("r", 5, TTL, agent_id="another-agent") == []
    claimed = await world.ledger.claim_reconciliation("r", 5, TTL, agent_id="scheduling-demo")
    assert len(claimed) == 1


# --- conversation identity is explicit ---


async def test_a_conversation_id_cannot_be_reused_by_another_channel_or_contact(
    world: World,
) -> None:
    await world.inbox.insert_if_absent(event("e1", clock=world.clock))
    hijack = event("e2", "oi", clock=world.clock).model_copy(update={"contact_id": "someone-else"})
    with pytest.raises(ConversationIdentityConflictError):
        await world.inbox.insert_if_absent(hijack)
    other_channel = event("e3", "oi", clock=world.clock).model_copy(update={"channel_id": "sms"})
    with pytest.raises(ConversationIdentityConflictError):
        await world.inbox.insert_if_absent(other_channel)
    assert await world.count("inbox_events") == 1  # refused events were not stored


async def test_a_new_session_for_the_same_conversation_is_accepted(world: World) -> None:
    await world.inbox.insert_if_absent(event("e1", clock=world.clock))
    later = event("e2", "voltei", clock=world.clock).model_copy(update={"session_id": "sess-2"})
    assert await world.inbox.insert_if_absent(later) is True  # one session per row is a known debt


def _ctx() -> Any:
    from conversation_agent.core.models.tooling import ToolContext

    return ToolContext(
        tenant_id=IDENTITY.tenant_id, agent_id="a", agent_version="1",
        channel_id=IDENTITY.channel_id, conversation_id=IDENTITY.conversation_id,
        session_id=IDENTITY.session_id, contact_id=IDENTITY.contact_id, turn_id="t",
        invocation_id="inv", trace_id="tr",
    )  # fmt: skip
