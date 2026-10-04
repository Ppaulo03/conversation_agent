"""Phase 8: the first Pack. The same behavior, installed by two agents over two different APIs,
with nothing but bindings and mappings differing (INV-040)."""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

import pytest
from pydantic import ValidationError

from conftest import AgendaHandle, ApiHandle
from conversation_agent.adapters.manifest.pack_loader import DirectoryPackLoader
from conversation_agent.app.compile import main as compile_cli
from conversation_agent.core.compiler import (
    CompileError,
    agent_document,
    compile_manifest,
)
from conversation_agent.core.definitions.pack import EvalScenario, EvalTurn, PackManifest
from conversation_agent.core.definitions.schema_spec import schema_fingerprint
from conversation_agent.core.errors import DefinitionError
from conversation_agent.engine.evals import run_scenario
from support.builders import IDENTITY
from support.pack_hosts import (
    CLINIC,
    HOSTS,
    HOSTS_DIR,
    PACKS_DIR,
    HostSpec,
    bookings_of,
    catalog_for,
    compile_host,
    eval_engine,
    host_manifest,
    load_pack,
    pipeline_for,
)
from vertical_slice.wiring import load_compiled_agent

# --- the Pack is the proven behavior, extracted ---


def test_the_pack_loads_with_its_requirements_and_evals() -> None:
    pack = load_pack()
    assert (pack.name, pack.version) == ("generic-scheduling", "1.0.0")
    assert {c["name"] for c in pack.capabilities} == {
        "scheduling.availability",
        "scheduling.create",
        "scheduling.lookup_booking",
    }
    assert [r.capability for r in pack.requires][-1] == "scheduling.create"
    assert len(pack.evals) == 3 and "service_word" in pack.eval_variables
    assert pack.digest == load_pack().digest  # stable


def test_installing_the_pack_reproduces_the_agent_the_slice_proved() -> None:
    proven = load_compiled_agent().agent
    installed = compile_host("clinic").agent

    def contracts(agent: Any) -> dict[str, tuple[Any, ...]]:
        return {
            c.name: (
                c.risk,
                c.confirmation_required,
                c.summary_template,
                schema_fingerprint(c.input_model, docs=False),
                schema_fingerprint(c.output_model, docs=False),
            )
            for c in agent.capabilities
        }

    assert contracts(installed) == contracts(proven)  # same capability contracts

    def bare(flows: Any) -> Any:  # the Flow's one-line description is prose, not behavior
        return tuple(f.model_copy(update={"description": ""}) for f in flows)

    assert bare(installed.flows) == bare(proven.flows)  # the very same Flow
    assert agent_document(installed)["tools"] == agent_document(proven)["tools"]
    assert agent_document(installed)["bindings"] == agent_document(proven)["bindings"]
    assert installed.allowed_capabilities == proven.allowed_capabilities


def test_the_compiled_agent_is_self_contained_and_records_what_it_installed() -> None:
    compiled = compile_host("clinic")
    pack = load_pack()
    manifest = compiled.manifest
    assert manifest is not None and manifest.packs == ()  # nothing left to install
    assert [(p.name, p.version, p.digest) for p in manifest.pack_lock] == [
        ("generic-scheduling", "1.0.0", pack.digest)
    ]
    again = compile_manifest(manifest)  # the runtime/registry path: no Pack, no catalog
    assert (again.digest, again.manifest_digest) == (compiled.digest, compiled.manifest_digest)


def test_a_changed_pack_changes_the_publication_but_not_what_the_agent_does() -> None:
    raw = host_manifest("clinic")
    pack = load_pack()
    edited = pack.model_copy(update={"description": "Same behavior, reworded."})
    first = compile_manifest(copy.deepcopy(raw), catalog_for(raw))
    second = compile_manifest(copy.deepcopy(raw), {("generic-scheduling", "1.0.0"): edited})
    assert first.digest == second.digest  # behavior identical
    assert first.manifest_digest != second.manifest_digest  # a different artifact was installed


def test_the_pack_contributes_prompt_rules_and_the_host_keeps_its_persona() -> None:
    persona = compile_host("clinic").agent.persona
    assert persona.startswith("Você é a assistente virtual de agendamentos")
    assert "## generic-scheduling: how to schedule" in persona
    assert "nunca invente horários" in persona


def test_the_pack_never_widens_what_the_model_may_call() -> None:
    def exposed(allow: list[str]) -> set[str]:
        def mutate(raw: dict[str, Any]) -> None:
            raw["packs"][0]["allow"] = allow

        compiled = compile_host("clinic", mutate)
        pipeline = pipeline_for(compiled, "http://unused", "scheduling_api")
        return {t.name for t in pipeline.exposed_tools()}

    both = ["scheduling.availability", "scheduling.create"]
    assert exposed(both) == {"scheduling__availability", "scheduling__create"}  # not lookup
    assert exposed([*both, "scheduling.lookup_booking"]) >= {"scheduling__lookup_booking"}


def test_a_pack_flow_cannot_run_a_capability_the_agent_did_not_allow() -> None:
    def allow_nothing(raw: dict[str, Any]) -> None:
        raw["packs"][0]["allow"] = []

    with pytest.raises(CompileError, match="not defined, bound and allowed"):
        compile_host("clinic", allow_nothing)


def test_a_host_binding_cannot_lower_what_the_pack_demands() -> None:
    def lower(raw: dict[str, Any]) -> None:
        for binding in raw["bindings"]:
            if binding["capability"] == "scheduling.create":
                binding["risk"] = "read"

    assert "RISK_DOWNGRADE" in codes(lower)  # the compiler's rule applies to Pack capabilities too


# --- installation is checked, never guessed ---


def codes(mutate: Callable[[dict[str, Any]], None]) -> set[str]:
    with pytest.raises(CompileError) as caught:
        compile_host("clinic", mutate)
    return caught.value.codes


def pack_use(raw: dict[str, Any]) -> dict[str, Any]:
    return raw["packs"][0]  # type: ignore[no-any-return]


def tool(raw: dict[str, Any], name: str) -> dict[str, Any]:
    return next(t for t in raw["tools"] if t["name"] == name)


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda r: pack_use(r).update(version="9.9.9"), "PACK_NOT_FOUND"),
        (lambda r: pack_use(r).update(name="no-such-pack"), "PACK_NOT_FOUND"),
        (lambda r: r["packs"].append(copy.deepcopy(r["packs"][0])), "PACK_DUPLICATE"),
        (lambda r: pack_use(r)["parameters"].pop("service_question"), "PACK_PARAMETER_MISSING"),
        (lambda r: pack_use(r)["parameters"].pop("services"), "PACK_PARAMETER_MISSING"),
        (lambda r: pack_use(r)["parameters"].update(bogus=1), "PACK_PARAMETER_UNKNOWN"),
        (lambda r: pack_use(r)["parameters"].update(services={}), "PACK_PARAMETER_INVALID"),
        (
            lambda r: pack_use(r)["parameters"]["services"]["haircut"].update(duration_minutes=0),
            "PACK_PARAMETER_INVALID",
        ),
        (
            lambda r: pack_use(r)["parameters"]["services"]["haircut"].update(surprise=1),
            "PACK_PARAMETER_INVALID",
        ),
        (
            lambda r: pack_use(r)["parameters"]["services"].update(
                Bad_Key={"keywords": [], "duration_minutes": 5}
            ),
            "PACK_PARAMETER_INVALID",
        ),
        (
            lambda r: pack_use(r).update(
                parameters={**pack_use(r)["parameters"], "service_question": 7}
            ),
            "PACK_PARAMETER_INVALID",
        ),
        (
            lambda r: pack_use(r).update(allow=["scheduling.delete_everything"]),
            "PACK_ALLOW_UNKNOWN",
        ),
        (lambda r: pack_use(r).update(unexpected=True), "PACK_USE_INVALID"),
    ],
)
def test_a_bad_installation_is_refused_by_name(
    mutate: Callable[[dict[str, Any]], None], code: str
) -> None:
    assert code in codes(mutate)


def test_a_pack_never_shadows_or_overrides_what_the_agent_defines() -> None:
    def define_create(raw: dict[str, Any]) -> None:
        raw["capabilities"] = [
            {
                "name": "scheduling.create",
                "description": "my own",
                "risk": "read",
                "input": {"x": {"type": "string"}},
                "output": {"y": {"type": "string"}},
            }
        ]

    def define_flow(raw: dict[str, Any]) -> None:
        raw["flows"] = [{"name": "scheduling"}]

    assert "PACK_NAME_COLLISION" in codes(define_create)
    assert "PACK_NAME_COLLISION" in codes(define_flow)


@pytest.mark.parametrize(
    ("mutate", "fragment"),
    [
        (
            lambda r: r.update(
                bindings=[
                    b for b in r["bindings"] if b["capability"] != "scheduling.lookup_booking"
                ]
            ),
            "no binding",
        ),
        (
            lambda r: tool(r, "erp_create_reservation").update(idempotency_supported=False),
            "idempotency",
        ),
        (lambda r: tool(r, "erp_create_reservation").pop("recovery"), "human_handoff"),
        (
            lambda r: tool(r, "erp_create_reservation")["recovery"].update(
                lookup_capability="scheduling.availability"
            ),
            "look up through",
        ),
        (
            lambda r: r.update(
                bindings=[
                    {**b, "tool": "ghost"} if b["capability"] == "scheduling.create" else b
                    for b in r["bindings"]
                ]
            ),
            "unknown tool",
        ),
    ],
)
def test_what_the_pack_requires_of_the_agent_is_checked_before_anything_runs(
    mutate: Callable[[dict[str, Any]], None], fragment: str
) -> None:
    with pytest.raises(CompileError) as caught:
        compile_host("clinic", mutate)
    unmet = [d for d in caught.value.diagnostics if d.code == "PACK_REQUIREMENT_UNMET"]
    assert unmet and fragment in unmet[0].message


def test_a_pack_that_needs_a_newer_framework_is_refused() -> None:
    raw = host_manifest("clinic")
    newer = load_pack().model_copy(update={"min_framework": "99.0.0"})
    with pytest.raises(CompileError) as caught:
        compile_manifest(raw, {("generic-scheduling", "1.0.0"): newer})
    assert "PACK_INCOMPATIBLE_FRAMEWORK" in caught.value.codes


def test_installing_without_a_catalog_names_the_missing_pack() -> None:
    with pytest.raises(CompileError) as caught:
        compile_manifest(host_manifest("clinic"))
    assert caught.value.codes == {"PACK_NOT_FOUND"}


# --- what a Pack is allowed to be ---


MINIMAL = {"name": "tiny-pack", "version": "1.0.0"}


@pytest.mark.parametrize(
    "forbidden", ["tools", "bindings", "connections", "allowed_capabilities", "persona", "secrets"]
)
def test_a_pack_has_no_place_for_tools_bindings_connections_or_secrets(forbidden: str) -> None:
    with pytest.raises(ValidationError):
        PackManifest.model_validate({**MINIMAL, forbidden: []})


@pytest.mark.parametrize(
    ("template", "message"),
    [
        ({"x": {"$param": "nope"}}, "not a declared parameter"),
        ({"x": {"$param": "one", "project": "f"}}, "cannot be projected"),
        ({"x": {"$param": "catalog", "project": "missing"}}, "no field"),
        ({"x": {"$param": "catalog", "oops": 1}}, "only takes"),
    ],
)
def test_a_template_may_only_reference_what_the_pack_declares(
    template: dict[str, Any], message: str
) -> None:
    pack = {
        **MINIMAL,
        "parameters": {
            "one": {"value": {"type": "string"}},
            "catalog": {"map_of": {"f": {"type": "string"}}},
        },
        "capabilities": [template],
    }
    with pytest.raises(ValidationError, match=message):
        PackManifest.model_validate(pack)


def test_a_parameter_has_exactly_one_shape() -> None:
    for parameters in (
        {"p": {}},
        {"p": {"value": {"type": "string"}, "map_of": {"f": {"type": "string"}}}},
    ):
        with pytest.raises(ValidationError):
            PackManifest.model_validate({**MINIMAL, "parameters": parameters})


# --- the loader and the CLI ---


def test_the_loader_serves_exactly_the_requested_pack_from_its_directory() -> None:
    loader = DirectoryPackLoader(PACKS_DIR)
    assert loader.load("generic-scheduling", "1.0.0").name == "generic-scheduling"
    for name, version in (
        ("generic-scheduling", "2.0.0"),
        ("nope", "1.0.0"),
        ("../packs", "1.0.0"),
    ):
        with pytest.raises(DefinitionError):
            loader.load(name, version)
    assert loader.catalog_for([{"name": "nope", "version": "1"}, "junk"]) == {}


def test_the_compile_cli_installs_packs_from_a_directory(
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest = str(HOSTS_DIR / "studio.yaml")
    assert compile_cli([manifest, "--packs", str(PACKS_DIR)]) == 0
    assert "OK studio-demo 0.1.0" in capsys.readouterr().out
    assert compile_cli([manifest]) == 1  # no catalog: the Pack cannot be found
    assert "PACK_NOT_FOUND" in capsys.readouterr().err
    assert compile_cli([manifest, "--packs"]) == 2


# --- the same Pack, two APIs ---


def api_of(request: pytest.FixtureRequest, host: HostSpec) -> Any:
    return request.getfixturevalue(host.fixture)


@pytest.mark.parametrize("host", HOSTS, ids=lambda h: h.name)
async def test_every_eval_of_the_pack_passes_on_each_host_against_its_real_api(
    host: HostSpec,
    request: pytest.FixtureRequest,
    api: ApiHandle,
    agenda: AgendaHandle,
) -> None:
    handle = api_of(request, host)
    other = agenda if host is CLINIC else api
    compiled = compile_host(host.name)
    pack = load_pack()
    for scenario in pack.evals:
        engine = eval_engine(compiled, handle.base_url, host.connection)
        result = await run_scenario(
            scenario,
            engine,
            IDENTITY,
            turn_prefix=f"{host.name}-{scenario.name}",
            variables={"service_word": host.service_word},
        )
        assert result.passed, (scenario.name, result.failures, result.transcript)
    paths = {r["path"] for r in handle.requests}
    assert host.first_path in paths  # it really searched ITS system
    assert other.requests == []  # and never the other one
    assert bookings_of(handle) == {}  # evals judge the conversation: nothing was booked


async def test_the_two_hosts_talk_to_their_apis_in_their_own_words(
    api: ApiHandle, agenda: AgendaHandle
) -> None:
    pack = load_pack()
    scenario = pack.evals[0]
    for host, handle in ((CLINIC, api), (HOSTS[1], agenda)):
        engine = eval_engine(compile_host(host.name), handle.base_url, host.connection)
        done = await run_scenario(
            scenario, engine, IDENTITY, turn_prefix=host.name,
            variables={"service_word": host.service_word},
        )  # fmt: skip
        assert done.passed
    (search,) = api.requests
    assert search["query"]["service_code"] == "HC-01"  # type: ignore[index]
    (found,) = agenda.requests
    assert found["query"]["servico"] == "MSG" and "de" in found["query"]  # type: ignore[operator,index]


async def test_an_eval_reports_every_expectation_that_fails(api: ApiHandle) -> None:
    broken = EvalScenario(
        name="wrong",
        turns=(
            EvalTurn(
                user="Quero agendar",
                reply_contains=("NOPE",),
                reply_not_contains=("Qual",),
                reply_matches=("^zzz",),
                proposes="scheduling.create",
            ),
            EvalTurn(user="corte", capture={"x": "will-not-match"}),
        ),
    )
    engine = eval_engine(compile_host("clinic"), api.base_url, "scheduling_api")
    result = await run_scenario(broken, engine, IDENTITY, turn_prefix="wrong")
    assert not result.passed and len(result.failures) >= 4
    assert any("lacks 'NOPE'" in f for f in result.failures)
    assert any("expected to propose" in f for f in result.failures)
    assert any("nothing to capture" in f for f in result.failures)
