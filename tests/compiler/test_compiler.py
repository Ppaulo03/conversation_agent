"""Phase 5 DoD: the compiler finds missing references, incompatible schemas, risk downgrades,
invalid bindings and incompatible versions BEFORE the runtime ever sees the agent."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from conversation_agent.adapters.manifest.yaml_loader import (
    MAX_MANIFEST_BYTES,
    load_manifest_file,
    parse_manifest_yaml,
)
from conversation_agent.core.compiler import (
    CompileError,
    agent_digest,
    compile_agent,
    compile_manifest,
)
from conversation_agent.core.definitions.mapping import TRANSFORM_NAMES
from conversation_agent.core.definitions.schema_spec import (
    FieldSpec,
    build_model,
    schema_fingerprint,
)
from conversation_agent.core.errors import DefinitionError
from conversation_agent.tools.mapping import TRANSFORMS
from vertical_slice.definitions import build_agent
from vertical_slice.wiring import MANIFEST_PATH, load_compiled_agent


def raw() -> dict[str, Any]:
    return copy.deepcopy(load_manifest_file(MANIFEST_PATH))


def codes_of(manifest: dict[str, Any]) -> set[str]:
    with pytest.raises(CompileError) as caught:
        compile_manifest(manifest)
    return caught.value.codes


def binding(manifest: dict[str, Any], capability: str) -> dict[str, Any]:
    return next(b for b in manifest["bindings"] if b["capability"] == capability)


def capability(manifest: dict[str, Any], name: str) -> dict[str, Any]:
    return next(c for c in manifest["capabilities"] if c["name"] == name)


# --- the two spellings are one agent ---


def test_the_manifest_and_the_python_objects_compile_to_the_same_agent() -> None:
    from_yaml = load_compiled_agent()
    from_python = compile_agent(build_agent(flows=True))
    assert from_yaml.digest == from_python.digest
    assert from_yaml.manifest is not None and from_python.manifest is None


def test_the_digest_ignores_the_version_label_but_not_the_content() -> None:
    a = load_compiled_agent()
    bumped = raw() | {"version": "0.2.0"}
    assert compile_manifest(bumped).digest == a.digest  # same content, new label
    changed = raw()
    changed["persona"] += "Seja breve."
    assert compile_manifest(changed).digest != a.digest


def test_transform_names_known_to_the_compiler_are_exactly_the_runtime_ones() -> None:
    assert frozenset(TRANSFORMS) == TRANSFORM_NAMES


# --- missing references ---


def test_missing_references_are_all_reported_in_one_pass() -> None:
    m = raw()
    m["bindings"][0]["capability"] = "scheduling.ghost"
    m["bindings"][1]["tool"] = "erp_ghost"
    m["allowed_capabilities"].append("scheduling.other")
    m["flows"][0]["steps"][1]["capability"] = "scheduling.phantom"
    found = codes_of(m)
    assert {"UNKNOWN_CAPABILITY", "UNKNOWN_TOOL"} <= found
    with pytest.raises(CompileError) as caught:
        compile_manifest(m)
    assert len(caught.value.diagnostics) >= 4  # not "first error wins"


def test_an_allowed_capability_without_a_binding_is_rejected() -> None:
    m = raw()
    m["bindings"] = [b for b in m["bindings"] if b["capability"] != "scheduling.create"]
    assert "UNBOUND_CAPABILITY" in codes_of(m)


def test_a_status_lookup_must_point_to_a_defined_bound_capability() -> None:
    m = raw()
    tool = next(t for t in m["tools"] if t["name"] == "erp_create_reservation")
    tool["recovery"]["lookup_capability"] = "scheduling.nowhere"
    assert "UNKNOWN_CAPABILITY" in codes_of(m)


def test_duplicate_names_and_bindings_are_rejected() -> None:
    m = raw()
    m["capabilities"].append(copy.deepcopy(m["capabilities"][0]))
    m["bindings"].append(copy.deepcopy(m["bindings"][0]))
    assert {"DUPLICATE_NAME", "DUPLICATE_BINDING"} <= codes_of(m)


# --- schema incompatibility ---


def test_mapping_to_a_field_the_tool_does_not_have_is_rejected() -> None:
    m = raw()
    binding(m, "scheduling.availability")["input_map"]["nope"] = "$.service_id"
    assert "MAPPING_UNKNOWN_TARGET" in codes_of(m)


def test_a_required_tool_argument_that_nothing_provides_is_rejected() -> None:
    m = raw()
    del binding(m, "scheduling.availability")["input_map"]["start"]
    assert "MAPPING_MISSING_REQUIRED" in codes_of(m)


def test_mapping_from_a_field_the_capability_does_not_have_is_rejected() -> None:
    m = raw()
    binding(m, "scheduling.availability")["input_map"]["start"] = "$.starting_day"
    assert "MAPPING_UNKNOWN_SOURCE" in codes_of(m)


def test_output_mapping_from_a_response_field_that_does_not_exist_is_rejected() -> None:
    m = raw()
    binding(m, "scheduling.create")["output_map"]["booking_id"] = "$.reservation_id"
    assert "MAPPING_UNKNOWN_SOURCE" in codes_of(m)


def test_a_required_capability_output_that_nothing_provides_is_rejected() -> None:
    m = raw()
    del binding(m, "scheduling.create")["output_map"]["status"]
    assert "MAPPING_MISSING_REQUIRED" in codes_of(m)


def test_an_unknown_transform_is_rejected() -> None:
    m = raw()
    binding(m, "scheduling.create")["input_map"]["hours"]["transform"] = "furlongs"
    assert "UNKNOWN_TRANSFORM" in codes_of(m)


def test_an_enum_mapping_must_cover_every_value_and_only_real_ones() -> None:
    m = raw()
    mapping = binding(m, "scheduling.availability")["input_map"]["service_code"]["enum"]
    del mapping["consultation"]
    mapping["surgery"] = "SG-01"
    assert {"ENUM_MAPPING_INCOMPLETE", "ENUM_MAPPING_UNKNOWN_KEY"} <= codes_of(m)


def test_a_flow_must_feed_a_capability_its_real_inputs() -> None:
    m = raw()
    inputs = m["flows"][0]["steps"][1]["inputs"]
    inputs["to_day"] = inputs.pop("to_date")
    assert {"FLOW_INPUT_UNKNOWN", "FLOW_INPUT_MISSING"} <= codes_of(m)


def test_a_flow_slot_cannot_offer_a_value_the_capability_rejects() -> None:
    m = raw()
    m["flows"][0]["slots"][0]["choices"]["surgery"] = ["cirurgia"]
    m["flows"][0]["steps"][3]["inputs"]["duration_minutes"]["mapping"]["surgery"] = 90
    assert "FLOW_ENUM_MISMATCH" in codes_of(m)


def test_a_summary_template_may_only_use_real_inputs() -> None:
    m = raw()
    capability(m, "scheduling.create")["summary_template"] = "Agendar {service} às {when}"
    assert "SUMMARY_UNKNOWN_FIELD" in codes_of(m)


def test_a_lookup_that_returns_another_schema_still_needs_a_result_map() -> None:
    m = raw()
    capability(m, "scheduling.lookup_booking")["output"]["extra"] = {"type": "string"}
    binding(m, "scheduling.lookup_booking")["output_map"]["extra"] = "$.state"
    assert "DEFINITION_INVALID" in codes_of(m)


# --- risk and invalid bindings ---


def test_a_binding_cannot_lower_the_risk() -> None:
    m = raw()
    binding(m, "scheduling.create")["risk"] = "read"
    assert "RISK_DOWNGRADE" in codes_of(m)


def test_an_ambiguous_write_cannot_be_mapped_to_a_retryable_error() -> None:
    m = raw()
    em = binding(m, "scheduling.create")["error_map"]
    em["timeout"]["write"] = {"type": "timeout", "retryable": True}
    em["http_status"][503] = {"type": "technical_error"}
    with pytest.raises(CompileError) as caught:
        compile_manifest(m)
    assert [d.code for d in caught.value.diagnostics].count("AMBIGUOUS_WRITE_ERROR_MAP") == 2


def test_an_impossible_http_status_in_an_error_map_is_rejected() -> None:
    m = raw()
    binding(m, "scheduling.availability")["error_map"]["http_status"][999] = {"type": "unknown"}
    assert "ERROR_MAP_INVALID_STATUS" in codes_of(m)


def test_semantic_definition_rules_surface_as_diagnostics() -> None:
    m = raw()
    tool = next(t for t in m["tools"] if t["name"] == "erp_get_available_slots")
    tool["recovery"] = {"strategy": "safe_retry"}
    binding(m, "scheduling.availability")["risk"] = "irreversible"
    assert "DEFINITION_INVALID" in codes_of(m)  # safe_retry on an effective write


# --- versions ---


def test_a_newer_manifest_format_is_refused() -> None:
    m = raw() | {"schema_version": 2}
    assert "INCOMPATIBLE_SCHEMA_VERSION" in codes_of(m)


def test_a_manifest_that_needs_a_newer_framework_is_refused() -> None:
    m = raw() | {"min_framework": "9.0.0"}
    assert "INCOMPATIBLE_FRAMEWORK" in codes_of(m)


def test_versions_must_be_strict_semver() -> None:
    for bad in ("1.0", "v1.0.0", "1.0.0-beta", "01.0.0"):
        assert "INVALID_VERSION" in codes_of(raw() | {"version": bad})


def test_a_python_agent_with_a_bad_version_is_refused_too() -> None:
    agent = build_agent().model_copy(update={"version": "latest"})
    with pytest.raises(CompileError):
        compile_agent(agent)


def test_unknown_manifest_keys_are_errors_not_ignored() -> None:
    m = raw() | {"persona_extra": "typo"}
    assert "MANIFEST_INVALID" in codes_of(m)


# --- YAML loading is data only ---


def test_yaml_rejects_duplicate_keys_python_tags_and_oversize_input() -> None:
    with pytest.raises(DefinitionError, match="duplicate key"):
        parse_manifest_yaml("a: 1\na: 2\n")
    with pytest.raises(DefinitionError, match="invalid YAML"):
        parse_manifest_yaml("a: !!python/object/apply:os.system ['echo hi']\n")
    with pytest.raises(DefinitionError, match="too large"):
        parse_manifest_yaml("a: " + "x" * MAX_MANIFEST_BYTES)
    with pytest.raises(DefinitionError, match="mapping"):
        parse_manifest_yaml("- just\n- a list\n")


def test_an_unquoted_on_key_is_read_as_the_word_on() -> None:
    assert parse_manifest_yaml("on:\n  x: 1\n") == {"on": {"x": 1}}


# --- the schema DSL ---


def test_a_built_model_is_strict_for_our_contracts_and_lenient_for_external_responses() -> None:
    spec = {"n": FieldSpec(type="integer", gt=0)}
    strict, lenient = build_model("S", spec), build_model("L", spec, strict=False)
    with pytest.raises(ValueError):
        strict.model_validate({"n": 1, "extra": 2})
    assert lenient.model_validate({"n": 1, "extra": 2}).model_dump() == {"n": 1}
    with pytest.raises(ValueError):
        strict.model_validate({"n": 0})  # gt=0


def test_the_fingerprint_ignores_class_names_and_nesting_style() -> None:
    a = build_model("A", {"x": FieldSpec(type="list", items=FieldSpec(type="string"))})
    b = build_model("B", {"x": FieldSpec(type="list", items=FieldSpec(type="string"))})
    assert schema_fingerprint(a) == schema_fingerprint(b)
    c = build_model("C", {"x": FieldSpec(type="list", items=FieldSpec(type="integer"))})
    assert schema_fingerprint(a) != schema_fingerprint(c)


def test_a_field_spec_must_be_internally_consistent() -> None:
    for bad in (
        {"type": "enum"},
        {"type": "string", "values": ["a"]},
        {"type": "list"},
        {"type": "object"},
        {"type": "string", "gt": 1},
    ):
        with pytest.raises(ValueError):
            FieldSpec.model_validate(bad)


def test_digest_is_stable_across_runs() -> None:
    assert agent_digest(build_agent(flows=True)) == agent_digest(build_agent(flows=True))


def test_a_flow_structure_error_is_reported_as_a_manifest_error() -> None:
    m = raw()
    m["flows"][0]["steps"].append({"kind": "collect", "id": "again", "slots": ["service"]})
    assert "MANIFEST_INVALID" in codes_of(m)  # Propose must be the last step
