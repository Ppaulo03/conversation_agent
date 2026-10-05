"""Operations (Phase 10): health read from the durable queues, Prometheus exposition, SLO artifacts
that cannot drift or point at nothing, readiness, and the integrity check used after a restore."""

from __future__ import annotations

import asyncio
import re
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from conftest import ApiHandle
from conversation_agent.adapters.clock import FixedClock
from conversation_agent.adapters.llm.fake import FakeLLM, text_response
from conversation_agent.adapters.observability.asgi import ops_app
from conversation_agent.adapters.observability.prometheus import (
    GAUGES,
    PREFIX,
    available_metrics,
    render,
)
from conversation_agent.adapters.observability.slo import (
    SLOFile,
    load,
    metrics_in,
    problems,
    render_dashboard,
    render_rules,
)
from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.adapters.postgres.health import HealthSnapshot, collect_health
from conversation_agent.adapters.postgres.integrity import verify_integrity
from conversation_agent.adapters.tools.metrics import InMemoryToolMetrics
from conversation_agent.app.integrity import main as integrity_cli
from conversation_agent.app.ops import main as ops_cli
from conversation_agent.core.models.runtime import ScheduledEvent
from postgres.test_protected_actions import answer, coord, deliver_prompt
from postgres.world import KEY, World, event

ROOT = Path(__file__).resolve().parents[2]
SLO_FILE = ROOT / "ops" / "slo.yaml"
RUNBOOK = ROOT / "docs" / "OPERATIONS.md"
TENANT = KEY.tenant_id


@pytest.fixture
def world(db: PostgresDatabase, clock: FixedClock) -> World:
    return World(db, clock)


# --- health from the durable queues ---


async def test_an_empty_database_is_all_zeros(db: PostgresDatabase) -> None:
    snapshot = await collect_health(db)
    assert snapshot == HealthSnapshot() and set(snapshot.as_dict().values()) == {0.0}


async def test_every_queue_shows_its_backlog_and_how_long_its_oldest_item_has_waited(
    world: World, api: ApiHandle
) -> None:
    pool = world.db.pool
    # a proposal waiting for confirmation, whose prompt is still to be sent
    await world.inbox.insert_if_absent(
        event("e-1", "Quero marcar um corte amanhã às 10h", clock=world.clock)
    )
    await coord(world, api, "w", FakeLLM([]), flows=True).process_conversation(KEY)
    await pool.execute(
        "UPDATE outbox_messages SET available_at = clock_timestamp() - interval '200 s'"
    )
    # an inbound event nobody has picked up yet
    await world.inbox.insert_if_absent(event("e-2", "alô?", clock=world.clock))
    await pool.execute(
        "UPDATE inbox_events SET received_at = clock_timestamp() - interval '90 s' "
        "WHERE event_id = 'e-2'"
    )
    # a handoff waiting for a person (set directly: through the service it would also end the
    # confirmation above, INV-060, and this test measures each queue on its own)
    await pool.execute(
        "UPDATE conversation_states SET ownership = 'HANDOFF_PENDING', "
        "updated_at = clock_timestamp() - interval '700 s'"
    )
    # a timer that should have fired
    await world.scheduler.schedule(
        ScheduledEvent(tenant_id=TENANT, scheduler_key="t-1", event_type="x",
                       due_at=world.clock.now() - timedelta(days=3650)),
    )  # fmt: skip
    await pool.execute("UPDATE scheduled_events SET due_at = clock_timestamp() - interval '300 s'")

    snap = await collect_health(world.db)
    assert snap.inbox_ready == 1 and 85 < snap.inbox_oldest_ready_age_seconds < 130
    assert snap.outbox_pending == 1 and 195 < snap.outbox_oldest_pending_age_seconds < 240
    assert snap.pending_confirmations == 1
    assert snap.handoffs_pending == 1 and 695 < snap.handoffs_oldest_pending_age_seconds < 760
    assert snap.scheduled_overdue == 1 and 295 < snap.scheduled_oldest_overdue_age_seconds < 340
    assert snap.turns_processing == 0 and snap.invocations_unresolved == 0


async def test_an_external_effect_with_an_unknown_outcome_is_visible_with_its_age(
    world: World, api: ApiHandle
) -> None:
    await world.inbox.insert_if_absent(
        event("e-1", "Quero marcar um corte amanhã às 10h", clock=world.clock)
    )
    await coord(world, api, "w", FakeLLM([]), flows=True).process_conversation(KEY)
    await deliver_prompt(world)
    api.state.fault = {"status_after_effect": 503}  # the booking exists, the answer was lost
    await answer(world, api, "sim", FakeLLM([text_response("...")]), flows=True)
    api.state.fault = None
    await world.db.pool.execute(
        "UPDATE tool_invocations SET prepared_at = clock_timestamp() - interval '1000 s' "
        "WHERE capability = 'scheduling.create'"
    )
    snap = await collect_health(world.db)
    assert (
        snap.invocations_unresolved == 1
        and 995 < snap.invocations_oldest_unresolved_age_seconds < 1040
    )


# --- the exposition format ---


def parse(text: str) -> dict[str, list[tuple[str, float]]]:
    """name -> [(labels, value)] from Prometheus text, strictly: every sample has HELP and TYPE."""
    helped, typed, samples = set(), set(), {}
    for line in text.strip().split("\n"):
        if line.startswith("# HELP "):
            helped.add(line.split()[2])
        elif line.startswith("# TYPE "):
            typed.add(line.split()[2])
        else:
            match = re.fullmatch(r"([a-z_0-9]+)(\{.*\})? (\S+)", line)
            assert match, f"not a sample line: {line!r}"
            samples.setdefault(match[1], []).append((match[2] or "", float(match[3])))
    assert set(samples) <= helped & typed
    return samples


def test_every_health_gauge_is_exposed_with_help_and_type() -> None:
    samples = parse(render(HealthSnapshot(inbox_ready=3, inbox_oldest_ready_age_seconds=12.5)))
    assert set(samples) == {PREFIX + g for g in GAUGES}
    assert samples[PREFIX + "inbox_ready"] == [("", 3.0)]
    assert samples[PREFIX + "inbox_oldest_ready_age_seconds"] == [("", 12.5)]


def test_tool_and_circuit_counters_are_exposed_with_safe_labels() -> None:
    tools = InMemoryToolMetrics()
    tools.record_call(provider="mcp", tool="a", status="success", error_code=None, duration_ms=120)
    tools.record_call(provider="mcp", tool="a", status="success", error_code=None, duration_ms=80)
    tools.record_call(
        provider="http", tool='we"ird\\na\nme', status="unknown", error_code="X", duration_ms=5
    )
    tools.record_circuit(provider="http", scope="erp", state="open")
    text = render(HealthSnapshot(), tools)
    samples = parse(text)
    assert ('{provider="mcp",tool="a",status="success",error_code=""}', 2.0) in samples[
        PREFIX + "tool_calls_total"
    ]
    assert ('{provider="mcp",tool="a"}', 0.2) in samples[PREFIX + "tool_call_duration_seconds_sum"]
    assert ('{provider="mcp",tool="a"}', 0.12) in samples[PREFIX + "tool_call_duration_seconds_max"]
    assert samples[PREFIX + "circuit_transitions_total"] == [
        ('{provider="http",scope="erp",state="open"}', 1.0)
    ]
    assert 'tool="we\\"ird\\\\na\\nme"' in text  # quotes, backslashes and newlines are escaped
    assert set(samples) <= available_metrics()  # nothing is exposed that the SLOs cannot know


# --- SLOs: one source, no drift, no dangling metrics or runbooks ---


def test_the_committed_alert_rules_and_dashboard_are_what_the_slo_file_generates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(ROOT)
    assert ops_cli(["check"]) == 0
    slos = load(SLO_FILE.read_text(encoding="utf-8"))
    assert (ROOT / "ops" / "alerts.rules.yml").read_text(encoding="utf-8") == render_rules(slos)
    assert (ROOT / "ops" / "grafana-dashboard.json").read_text(
        encoding="utf-8"
    ) == render_dashboard(slos)

    ops_cli(["render", "--out", str(tmp_path)])
    (tmp_path / "alerts.rules.yml").write_text("edited by hand", encoding="utf-8")
    assert ops_cli(["check", "--out", str(tmp_path)]) == 1 and "stale" in capsys.readouterr().err
    assert (
        ops_cli(["bogus"]) == 2 and ops_cli(["check", "--slo", str(tmp_path / "missing.yaml")]) == 2
    )


def test_every_slo_uses_real_metrics_and_names_a_runbook_section_that_exists() -> None:
    slos = load(SLO_FILE.read_text(encoding="utf-8"))
    assert problems(slos, RUNBOOK.read_text(encoding="utf-8")) == []
    assert all(metrics_in(s.expr) <= available_metrics() for s in slos.slos)


def test_an_alert_on_a_missing_metric_or_without_a_runbook_is_refused() -> None:
    def one(**changes: Any) -> SLOFile:
        base = {"name": "X", "description": "d", "expr": f"{PREFIX}inbox_ready > 1", "for": "5m",
                "severity": "warning", "runbook": "inbox-latency"}  # fmt: skip
        return SLOFile.model_validate({"slos": [{**base, **changes}]})

    runbook = RUNBOOK.read_text(encoding="utf-8")
    assert problems(one(), runbook) == []
    assert "unknown metric" in problems(one(expr=f"{PREFIX}made_up > 1"), runbook)[0]
    assert "uses no conversation_agent metric" in problems(one(expr="up == 0"), runbook)[0]
    assert "runbook anchor #nowhere" in problems(one(runbook="nowhere"), runbook)[0]
    dup = SLOFile.model_validate({"slos": [one().slos[0].model_dump(by_alias=True)] * 2})
    assert "duplicate SLO name" in problems(dup, runbook)[0]
    with pytest.raises(ValueError):
        one(**{"for": "soon"})


# --- readiness and the endpoints ---


async def test_the_ops_endpoints(db: PostgresDatabase, monkeypatch: pytest.MonkeyPatch) -> None:
    tools = InMemoryToolMetrics()
    tools.record_call(provider="mcp", tool="a", status="success", error_code=None, duration_ms=1)
    transport = httpx.ASGITransport(app=ops_app(db, tools))
    async with httpx.AsyncClient(transport=transport, base_url="http://ops") as client:
        assert (await client.get("/healthz")).json() == {"status": "alive"}
        assert (await client.get("/readyz")).json() == {"status": "ready"}
        metrics = await client.get("/metrics")
        assert metrics.status_code == 200 and metrics.headers["content-type"].startswith(
            "text/plain"
        )
        assert PREFIX + "inbox_ready 0" in metrics.text and "tool_calls_total" in metrics.text
        assert (await client.get("/nope")).status_code == 404
        assert (await client.post("/metrics")).status_code == 405

        from conversation_agent.adapters.postgres.migrator import Migrator, SchemaAheadError

        async def ahead(self: Migrator) -> None:
            raise SchemaAheadError("the database is ahead")

        monkeypatch.setattr(Migrator, "ensure_current", ahead)
        not_ready = await client.get("/readyz")
        assert not_ready.status_code == 503 and not_ready.json()["reason"] == "SchemaAheadError"


async def test_a_dead_database_is_not_ready_but_the_process_is_alive() -> None:
    dead = await PostgresDatabase.connect(
        "postgresql://conversation_agent:conversation_agent_dev@127.0.0.1:5432/conversation_agent_test",
        max_size=1,
    )
    await dead.close()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=ops_app(dead)), base_url="http://ops"
    ) as client:
        assert (await client.get("/healthz")).status_code == 200
        assert (await client.get("/readyz")).status_code == 503
        assert (await client.get("/metrics")).status_code == 503


# --- integrity ---


async def start(world: World) -> None:
    await world.inbox.insert_if_absent(event("e0", "oi", clock=world.clock))
    await world.coordinator("c", FakeLLM([text_response("Olá!")])).run_once()
    await world.outbox_worker("s").run_once()


async def test_a_healthy_database_has_no_violation(world: World, api: ApiHandle) -> None:
    await start(world)
    report = await verify_integrity(world.db)
    assert report.ok and len(report.checked) >= 8 and report.violations == ()


VIOLATIONS: dict[str, str] = {
    "confirmed_action_without_invocation": (
        "INSERT INTO pending_actions (tenant_id, conversation_id, action_id, capability, "
        "tool_name, request_json, args_hash, protected_fields, summary, created_from_turn, "
        "status, created_at, updated_at) VALUES ('tenant-1','conv-1','orphan','c','t','{}',"
        "'h','[]','s','turn','CONFIRMED',"
        "now(),now())"
    ),
    "repeated_idempotency_key": (
        "INSERT INTO outbox_messages SELECT tenant_id, outbox_id || '-copy', conversation_id, "
        "channel_id, contact_id, turn_id, message_index + 100, action_id, text, "
        "idempotency_key, status, attempts, "
        "available_at, claim_owner, claim_expires_at, provider_message_id, provider_accepted_at, "
        "last_error, created_at, updated_at, first_sent_at, reconcile_attempts, channel_message_id "
        "FROM outbox_messages"
    ),
    "conversation_pinned_to_unpublished_version": (
        "UPDATE conversation_states SET agent_id = 'ghost', agent_version = '9.9.9'"
    ),
    "consumed_event_without_turn": "UPDATE inbox_events SET status = 'CONSUMED', turn_id = NULL",
    "lease_without_expiry": (
        "UPDATE conversation_states SET lease_owner = 'zombie', lease_expires_at = NULL"
    ),
    "release_points_to_unpublished_version": (
        "INSERT INTO agent_release_state (tenant_id, agent_id, stable) "
        "VALUES ('tenant-1','ghost','1.0.0')"
    ),
    "release_stable_is_withdrawn": (
        "INSERT INTO agent_release_state (tenant_id, agent_id, stable, withdrawn) "
        "VALUES ('tenant-1','other','1.0.0', ARRAY['1.0.0'])"
    ),
}


@pytest.mark.parametrize("code", sorted(VIOLATIONS))
async def test_each_corruption_is_found_by_its_own_check_and_only_that_one(
    world: World, code: str
) -> None:
    await start(world)
    await world.db.pool.execute(VIOLATIONS[code])
    report = await verify_integrity(world.db)
    found = {v.code: v for v in report.violations}
    assert code in found and found[code].count >= 1 and found[code].sample
    assert not report.ok
    others = set(found) - {code}
    assert (
        others <= {"release_points_to_unpublished_version"}
        if code == "release_stable_is_withdrawn"
        else not others
    )


async def test_a_wrong_schema_is_reported_before_any_check_runs(world: World) -> None:
    await world.db.pool.execute(
        "UPDATE schema_migrations SET checksum = 'edited' WHERE version = '0001_init'"
    )
    report = await verify_integrity(world.db)
    assert (
        not report.ok
        and report.violations == ()
        and "MigrationDriftError" in (report.schema_problem or "")
    )
    await world.db.pool.execute(
        "UPDATE schema_migrations SET checksum = NULL WHERE version = '0001_init'"
    )  # leave the shared database as the other tests expect it


async def test_the_integrity_cli_reports_and_exits_accordingly(
    world: World, pg_dsn: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    await start(world)
    monkeypatch.setenv("DATABASE_URL", pg_dsn)
    loop = asyncio.get_running_loop()

    async def cli(*args: str) -> int:
        return await loop.run_in_executor(None, integrity_cli, list(args))

    assert await cli() == 0 and "ok:" in capsys.readouterr().out
    await world.db.pool.execute(VIOLATIONS["lease_without_expiry"])
    assert await cli() == 1 and "lease_without_expiry" in capsys.readouterr().err
    assert await cli("extra") == 2
    monkeypatch.delenv("DATABASE_URL")
    assert await cli() == 2
