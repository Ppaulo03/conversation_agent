"""INV-027: an operation prepared against one destination can never be sent, or recovered, against
another because configuration changed in between. The recovery lookup is frozen with the write;
its result is converted explicitly; and only the declared absence code proves "it never happened".
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from conftest import ApiHandle
from conversation_agent.adapters.clock import FixedClock
from conversation_agent.adapters.faults import ChaosFaults, SimulatedCrash
from conversation_agent.adapters.llm.fake import FakeLLM, tool_call_response
from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.adapters.tools.http import HTTPToolProvider, local_dev_connection
from conversation_agent.core.definitions.agent import AgentDefinition
from conversation_agent.core.definitions.binding import CapabilityBinding
from conversation_agent.core.definitions.capability import CapabilityDefinition
from conversation_agent.core.definitions.mapping import Const
from conversation_agent.core.definitions.tool import RecoverySpec
from conversation_agent.core.models.runtime import InvocationStatus, ToolInvocation
from conversation_agent.engine.capability_pipeline import CapabilityPipeline
from conversation_agent.engine.tool_runner import ToolRunner
from postgres.world import KEY, AllowWrites, World, event
from support.builders import make_pipeline
from vertical_slice.definitions import (
    CREATE_BINDING,
    ERP_CREATE_RESERVATION,
    ERP_FIND_RESERVATION,
    LOOKUP,
    LOOKUP_BINDING,
    build_agent,
)

BOOKING = {"service_id": "haircut", "start_at": "2026-10-06T10:00:00-03:00", "duration_minutes": 30}
ALLOW = frozenset({"scheduling.availability", "scheduling.create"})
CONNECTION = "scheduling_api"


@pytest.fixture
def world(db: PostgresDatabase, clock: FixedClock) -> World:
    return World(db, clock)


def posts(api: ApiHandle) -> list[dict[str, object]]:
    return [r for r in api.requests if r["method"] == "POST"]


def pipeline_for(
    provider: HTTPToolProvider, agent: AgentDefinition | None = None
) -> CapabilityPipeline:
    return make_pipeline(agent or build_agent(), AllowWrites(ALLOW), ToolRunner({"http": provider}))


def provider_at(base_url: str) -> HTTPToolProvider:
    return HTTPToolProvider.static({CONNECTION: local_dev_connection(base_url)})


async def prepare_then_crash(
    world: World,
    api: ApiHandle,
    pipeline: CapabilityPipeline,
    point: str = "C04_before_external_request",
) -> ToolInvocation:
    await world.inbox.insert_if_absent(event("e1", "terça 10h", clock=world.clock))
    with pytest.raises(SimulatedCrash):
        await world.coordinator(
            "w1",
            FakeLLM([tool_call_response("scheduling__create", BOOKING)]),
            pipeline=pipeline,
            faults=ChaosFaults(point),
        ).process_conversation(KEY)
    world.clock.set(world.clock.now() + timedelta(seconds=61))
    row = await world.db.pool.fetchval("SELECT invocation_id FROM tool_invocations")
    inv = await world.ledger.get(KEY.tenant_id, row)
    assert inv is not None
    return inv


# --- what is frozen ---


async def test_prepare_freezes_the_destination_and_the_whole_recovery_lookup(
    world: World, api: ApiHandle
) -> None:
    inv = await prepare_then_crash(world, api, pipeline_for(provider_at(api.base_url)))
    expected = local_dev_connection(api.base_url).fingerprint()
    assert inv.intent.connection_fingerprint == expected  # WHERE the write goes
    lookup = inv.intent.recovery.lookup_intent
    assert lookup is not None  # and the status lookup, frozen with the write
    assert lookup.tool_name == "erp_find_reservation"
    assert lookup.tool_args == {"key": inv.invocation_id}  # the runtime's key, never the LLM's
    assert lookup.connection_fingerprint == expected  # asks the SAME system


def test_the_connection_fingerprint_covers_the_destination_but_never_secrets() -> None:
    from conversation_agent.core.models.connections import AuthSpec, ResolvedConnection

    def conn(**kw: Any) -> ResolvedConnection:
        base = {"connection_id": "c", "base_url": "https://erp.example.com/v1"}
        return ResolvedConnection(**{**base, **kw})

    reference = conn().fingerprint()
    assert conn().fingerprint() == reference  # deterministic
    for moved in (
        conn(base_url="https://erp-new.example.com/v1"),  # host
        conn(base_url="https://erp.example.com:8443/v1"),  # port
        conn(base_url="https://erp.example.com/v2"),  # path
        conn(base_url="http://erp.example.com/v1", tls_required=False),  # scheme/TLS policy
        conn(allow_private_networks=True),  # network policy
        conn(allowed_hosts=frozenset({"erp.example.com", "other.example.com"})),
        conn(auth=AuthSpec(secret_ref="token")),  # a different auth *shape*
    ):
        assert moved.fingerprint() != reference
    # limits and timeouts are operational knobs, not destinations; rotating a secret VALUE (which
    # is not part of the connection at all) obviously cannot change it either.
    assert conn(max_timeout_seconds=1, max_response_bytes=10).fingerprint() == reference


# --- the write cannot move ---


async def test_a_prepared_write_is_never_sent_to_a_destination_it_was_not_prepared_for(
    world: World, api: ApiHandle
) -> None:
    inv = await prepare_then_crash(world, api, pipeline_for(provider_at(api.base_url)))
    # Same connection NAME, different system: the old fingerprint check (by name) would pass.
    elsewhere = pipeline_for(provider_at("http://127.0.0.1:9"))
    (final,) = await world.reconciler(
        "r", providers={"http": provider_at("http://127.0.0.1:9")}, pipeline=elsewhere
    ).run_once()
    assert final.invocation_id == inv.invocation_id
    assert final.status is InvocationStatus.HUMAN_HANDOFF  # the frozen lookup refuses to move
    assert final.result is not None
    assert final.result["error"]["code"] == "LOOKUP_DESTINATION_CHANGED"
    assert posts(api) == []  # nothing was (re)sent anywhere


async def test_a_resend_to_a_moved_destination_is_refused_even_without_a_lookup(
    world: World, api: ApiHandle
) -> None:
    no_lookup = ERP_CREATE_RESERVATION.model_copy(
        update={"recovery": RecoverySpec(strategy="retry_same_key")}
    )
    agent = build_agent().model_copy(
        update={
            "tools": tuple(
                no_lookup if t.name == no_lookup.name else t for t in build_agent().tools
            )
        }
    )
    inv = await prepare_then_crash(world, api, pipeline_for(provider_at(api.base_url), agent))
    assert inv.intent.recovery.lookup_intent is None
    moved = pipeline_for(provider_at("http://127.0.0.1:9"), agent)
    (final,) = await world.reconciler(
        "r", providers={"http": provider_at("http://127.0.0.1:9")}, pipeline=moved
    ).run_once()
    assert final.status is InvocationStatus.HUMAN_HANDOFF
    assert final.result is not None and final.result["error"]["code"] == "DESTINATION_CHANGED"
    assert posts(api) == []


async def test_the_execution_path_refuses_a_stale_destination_without_sending(
    api: ApiHandle,
) -> None:
    from contracts.test_tool_provider_contract import CONTEXT
    from conversation_agent.adapters.tools.http import HTTPToolProvider as Provider

    resolved = build_agent().resolve("scheduling.create")
    assert resolved is not None
    args = {"service_code": "HC-01", "starts_at": "2026-10-06T13:00:00+00:00", "hours": 0.5}
    result = await Provider.static({CONNECTION: local_dev_connection(api.base_url)}).execute(
        resolved, args, CONTEXT, destination_fingerprint="prepared-against-another-system"
    )
    assert result.status == "technical_error" and result.error is not None
    assert result.error.code == "DESTINATION_CHANGED" and api.requests == []


# --- the lookup cannot move ---


async def test_a_changed_lookup_binding_refuses_to_decide_absence(
    world: World, api: ApiHandle
) -> None:
    inv = await prepare_then_crash(world, api, pipeline_for(provider_at(api.base_url)))
    drifted = LOOKUP_BINDING.model_copy(update={"input_map": {"key": Const(const="other-key")}})
    agent = build_agent().model_copy(
        update={
            "bindings": tuple(
                drifted if b.capability == LOOKUP.name else b for b in build_agent().bindings
            )
        }
    )
    (final,) = await world.reconciler(
        "r",
        providers={"http": provider_at(api.base_url)},
        pipeline=pipeline_for(provider_at(api.base_url), agent),
    ).run_once()
    assert final.invocation_id == inv.invocation_id
    assert final.status is InvocationStatus.HUMAN_HANDOFF
    assert final.result is not None and final.result["error"]["code"] == "LOOKUP_CHANGED"
    assert posts(api) == []  # "not found" from a DIFFERENT query must never trigger a re-send


# --- lookup result -> original result ---


class LookupV2Output(BaseModel):
    model_config = ConfigDict(extra="forbid")
    exists: bool
    request_status: str
    external_reference: str


def agent_with_v2_lookup(result_map: dict[str, Any] | None) -> AgentDefinition:
    """A lookup whose output schema is NOT the create output: it must declare how to convert."""
    base = build_agent()
    lookup = CapabilityDefinition(
        name="scheduling.lookup_request",
        description="Finds a request by idempotency key (recovery).",
        input_model=LOOKUP.input_model,
        output_model=LookupV2Output,
        risk="read",
    )
    binding = CapabilityBinding(
        capability="scheduling.lookup_request",
        tool="erp_find_reservation",
        input_map={"key": "$.idempotency_key"},
        output_map={
            "exists": Const(const=True),
            "request_status": "$.state",
            "external_reference": "$.id",
        },
        error_map=LOOKUP_BINDING.error_map,
    )
    create = ERP_CREATE_RESERVATION.model_copy(
        update={
            "recovery": RecoverySpec(
                strategy="status_lookup",
                lookup_capability="scheduling.lookup_request",
                result_map=result_map,
                absent_codes=("BOOKING_NOT_FOUND",),
            )
        }
    )
    tools = tuple(create if t.name == create.name else t for t in base.tools)
    return base.model_copy(
        update={
            "capabilities": (*base.capabilities, lookup),
            "bindings": (*base.bindings, binding),
            "tools": tools,
        }
    )


GOOD_MAP = {"booking_id": "$.external_reference", "status": "$.request_status"}


async def test_a_lookup_with_a_different_schema_is_converted_by_the_declared_result_map(
    world: World, api: ApiHandle
) -> None:
    agent = agent_with_v2_lookup(GOOD_MAP)
    pipeline = pipeline_for(provider_at(api.base_url), agent)
    await world.inbox.insert_if_absent(event("e1", "terça 10h", clock=world.clock))
    api.state.fault = {"status_after_effect": 503}  # the booking exists; the answer was lost
    await world.coordinator(
        "w1", FakeLLM([tool_call_response("scheduling__create", BOOKING)]), pipeline=pipeline
    ).process_conversation(KEY)
    api.state.fault = None

    (final,) = await world.reconciler(
        "r", providers={"http": provider_at(api.base_url)}, pipeline=pipeline
    ).run_once()
    assert final.status is InvocationStatus.RECONCILED
    assert final.result is not None
    assert final.result["data"] == {"booking_id": "bk_1", "status": "confirmed"}  # converted
    assert len(posts(api)) == 1


async def test_a_lookup_result_that_cannot_become_the_original_result_is_refused(
    world: World, api: ApiHandle
) -> None:
    agent = agent_with_v2_lookup({"booking_id": "$.external_reference"})  # `status` is missing
    pipeline = pipeline_for(provider_at(api.base_url), agent)
    await world.inbox.insert_if_absent(event("e1", "terça 10h", clock=world.clock))
    api.state.fault = {"status_after_effect": 503}
    await world.coordinator(
        "w1", FakeLLM([tool_call_response("scheduling__create", BOOKING)]), pipeline=pipeline
    ).process_conversation(KEY)
    api.state.fault = None
    (final,) = await world.reconciler(
        "r", providers={"http": provider_at(api.base_url)}, pipeline=pipeline
    ).run_once()
    assert final.status is InvocationStatus.HUMAN_HANDOFF
    assert final.result is not None and final.result["error"]["code"] == "LOOKUP_RESULT_UNUSABLE"


# --- definition-time checks ---


def rebuild(agent: AgentDefinition, **updates: Any) -> AgentDefinition:
    fields = {k: getattr(agent, k) for k in AgentDefinition.model_fields}
    return AgentDefinition.model_validate({**fields, **updates})


def test_a_lookup_with_another_output_schema_needs_a_result_map() -> None:
    from conversation_agent.core.errors import DefinitionError

    with pytest.raises(DefinitionError, match="result_map"):
        rebuild(agent_with_v2_lookup(None))
    rebuild(agent_with_v2_lookup(GOOD_MAP))  # declared conversion: accepted


def test_a_status_lookup_must_exist_be_bound_be_a_read_and_take_the_idempotency_key() -> None:
    from conversation_agent.core.errors import DefinitionError

    base = build_agent()

    def with_recovery(**kw: Any) -> AgentDefinition:
        tool = ERP_CREATE_RESERVATION.model_copy(
            update={"recovery": RecoverySpec(strategy="status_lookup", **kw)}
        )
        return rebuild(base, tools=tuple(tool if t.name == tool.name else t for t in base.tools))

    with pytest.raises(DefinitionError, match="not defined and bound"):
        with_recovery(lookup_capability="scheduling.nope")
    with pytest.raises(DefinitionError, match="effective read"):
        with_recovery(lookup_capability="scheduling.create")

    class NoKey(BaseModel):
        pass

    keyless = LOOKUP.model_copy(update={"input_model": NoKey})
    caps = tuple(keyless if c.name == LOOKUP.name else c for c in base.capabilities)
    tool = ERP_CREATE_RESERVATION
    with pytest.raises(DefinitionError, match="idempotency_key"):
        rebuild(
            base,
            capabilities=caps,
            tools=tuple(tool if t.name == tool.name else t for t in base.tools),
        )


def test_retry_rules_follow_the_effective_risk_not_the_tool_label() -> None:
    from conversation_agent.core.definitions.tool import RetryPolicy
    from conversation_agent.core.errors import DefinitionError

    base = build_agent()
    # a tool that LOOKS like a read (retries, no idempotency) bound to an irreversible capability
    lookalike = ERP_FIND_RESERVATION.model_copy(
        update={"name": "erp_lookalike", "retry": RetryPolicy(max_attempts=3)}
    )
    binding = CREATE_BINDING.model_copy(update={"tool": "erp_lookalike"})
    with pytest.raises(DefinitionError, match="effective risk"):
        rebuild(
            base,
            tools=(*base.tools, lookalike),
            bindings=tuple(
                binding if b.capability == "scheduling.create" else b for b in base.bindings
            ),
        )
