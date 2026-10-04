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


def test_field_order_never_changes_a_schema_fingerprint() -> None:
    # JSONB does not keep key order, so a stored manifest reloads with its fields reordered
    a = build_model("A", {"x": FieldSpec(type="string"), "y": FieldSpec(type="integer")})
    b = build_model("B", {"y": FieldSpec(type="integer"), "x": FieldSpec(type="string")})
    assert schema_fingerprint(a) == schema_fingerprint(b)


def test_the_compile_command_reports_ok_and_diagnostics(
    tmp_path: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    from conversation_agent.app.compile import main

    assert main([str(MANIFEST_PATH)]) == 0
    assert "OK scheduling-demo 0.1.0 digest=" in capsys.readouterr().out
    bad = tmp_path / "bad.yaml"
    text = MANIFEST_PATH.read_text(encoding="utf-8").replace(
        "lookup_capability: scheduling.lookup_booking", "lookup_capability: ghost.cap"
    )
    bad.write_text(text, encoding="utf-8")
    assert main([str(bad)]) == 1
    assert "error(s)" in capsys.readouterr().err
    assert main([str(tmp_path / "missing.yaml")]) == 2


# --- Phase 5.1: types, scalars, outputs, sealing ---


def tool(manifest: dict[str, Any], name: str) -> dict[str, Any]:
    return next(t for t in manifest["tools"] if t["name"] == name)


def test_a_mapping_cannot_feed_a_value_of_the_wrong_type() -> None:
    m = raw()
    tool(m, "erp_get_available_slots")["input"]["start"] = {"type": "integer"}  # date -> integer
    with pytest.raises(CompileError) as caught:
        compile_manifest(m)
    mismatches = [d for d in caught.value.diagnostics if d.code == "MAPPING_TYPE_MISMATCH"]
    assert mismatches and "date" in mismatches[0].message and "integer" in mismatches[0].message


def test_a_transform_only_accepts_the_types_it_is_defined_for() -> None:
    m = raw()
    capability(m, "scheduling.create")["input"]["duration_minutes"] = {"type": "string"}
    assert "MAPPING_TRANSFORM_INPUT" in codes_of(m)  # minutes_to_hours needs a number


def test_a_transform_changes_the_type_that_reaches_the_target() -> None:
    m = raw()
    tool(m, "erp_create_reservation")["input"]["hours"] = {"type": "string"}  # hours is a number
    assert "MAPPING_TYPE_MISMATCH" in codes_of(m)


def test_compatible_coercions_are_not_errors() -> None:
    m = raw()  # datetime -> ISO string, string -> datetime, integer -> number are all in use
    assert compile_manifest(m).digest


def test_a_mapping_cannot_walk_beyond_a_scalar() -> None:
    m = raw()
    binding(m, "scheduling.create")["output_map"]["status"] = "$.state.foo"
    assert "MAPPING_SCALAR_TRAVERSAL" in codes_of(m)
    binding(m, "scheduling.create")["output_map"]["status"] = "$.state[0]"
    assert "MAPPING_SCALAR_TRAVERSAL" in codes_of(m)  # and a scalar is not a list either


def test_a_tool_without_output_cannot_feed_a_capability_that_needs_one() -> None:
    m = raw()
    tool(m, "erp_create_reservation")["output"] = None
    assert "TOOL_HAS_NO_OUTPUT" in codes_of(m)


def test_a_flow_cannot_feed_a_capability_the_wrong_type() -> None:
    m = raw()
    capability(m, "scheduling.availability")["input"]["to_date"] = {"type": "integer"}
    assert "FLOW_TYPE_MISMATCH" in codes_of(m)


def test_only_the_compiler_can_produce_a_compiled_agent() -> None:
    from conversation_agent.core.compiler import CompiledAgent

    with pytest.raises(TypeError, match="only be produced by the compiler"):
        CompiledAgent(agent=build_agent(), digest="x", manifest=None)


# --- Phase 5.2: confirmation texts, structural types, defaults ---


def test_a_protected_agent_cannot_turn_the_confirmation_question_off() -> None:
    m = raw() | {"confirmation_prompt_enabled": False}
    with pytest.raises(CompileError, match="confirmation_prompt_enabled=false"):
        compile_manifest(m)


def test_the_confirmation_question_can_be_off_when_nothing_is_protected() -> None:
    m = raw() | {"confirmation_prompt_enabled": False}
    m["allowed_capabilities"] = ["scheduling.availability"]
    m["flows"] = []
    assert compile_manifest(m).agent.confirmation_prompt_enabled is False


@pytest.mark.parametrize(
    ("key", "text", "message"),
    [
        ("prompt", "Pode confirmar?", "must contain"),  # says nothing about WHAT
        ("prompt", "{foo}", "unknown placeholder"),
        ("reprompt", "Não entendi. Responda SIM ou NÃO.", "must contain"),
        ("prompt", "Confirma {summary", "malformed"),
        ("prompt", "Confirma {summary.x}?", "plain name"),
        ("executed_fallback", "Feito: {summary}", "unknown placeholder"),
        ("rejected", "Cancelado {x}", "unknown placeholder"),
    ],
)
def test_confirmation_texts_must_be_formattable_and_say_what_is_confirmed(
    key: str, text: str, message: str
) -> None:
    m = raw()
    m["confirmation"][key] = text
    with pytest.raises(CompileError, match=message):
        compile_manifest(m)


@pytest.mark.parametrize("text", ["Oi {nope}", "Dia {date", "Dia {date.year}", "{0}"])
def test_flow_texts_cannot_hold_templates_that_would_raise_in_a_conversation(text: str) -> None:
    m = raw()
    m["flows"][0]["steps"][1]["default"]["text"] = text
    assert "MANIFEST_INVALID" in codes_of(m)


def test_flow_prompts_may_use_slots_and_the_options_placeholder() -> None:
    m = raw()
    m["flows"][0]["slots"][1]["prompt"] = "Qual dia para {service}?"
    assert compile_manifest(m).digest


def test_a_whole_list_cannot_fill_a_list_of_different_items() -> None:
    m = raw()
    binding(m, "scheduling.availability")["output_map"]["slots"] = (
        "$.items"  # {start,end}->{start_at,end_at}
    )
    with pytest.raises(CompileError) as caught:
        compile_manifest(m)
    assert "MAPPING_TYPE_MISMATCH" in caught.value.codes
    m = raw()
    tool(m, "erp_get_available_slots")["output"]["items"] = {
        "type": "list",
        "items": {"type": "string"},
    }
    binding(m, "scheduling.availability")["output_map"]["slots"] = "$.items"
    assert "MAPPING_TYPE_MISMATCH" in codes_of(m)


def test_an_object_cannot_fill_a_scalar_or_an_object_of_other_fields() -> None:
    m = raw()
    binding(m, "scheduling.availability")["output_map"]["next_cursor"] = "$.pagination"
    with pytest.raises(CompileError, match="MAPPING_TYPE_MISMATCH"):
        compile_manifest(m)


def test_structurally_compatible_objects_are_accepted() -> None:
    m = raw()
    capability(m, "scheduling.availability")["output"]["next_cursor"] = {
        "type": "object",
        "required": False,
        "fields": {"next_cursor": {"type": "string", "required": False}},
    }
    binding(m, "scheduling.availability")["output_map"]["next_cursor"] = "$.pagination"
    assert compile_manifest(m).digest  # same fields: assignable


@pytest.mark.parametrize(
    "field",
    [
        {"type": "integer", "required": False, "default": "twenty"},
        {"type": "integer", "gt": 0, "default": 0},
        {"type": "integer", "required": False, "default": True},
        {"type": "string", "max_length": 2, "default": "abc"},
        {"type": "enum", "values": ["a", "b"], "default": "c"},
        {"type": "datetime", "default": "2026-10-06T10:00:00"},
        {"type": "date", "default": "amanhã"},
        {"type": "list", "items": {"type": "integer"}, "default": [1, "x"]},
        {"type": "integer", "default": None},
    ],
)
def test_a_default_must_satisfy_its_own_field(field: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="default"):
        FieldSpec.model_validate(field)


def test_valid_defaults_are_accepted_and_applied() -> None:
    spec = {
        "n": FieldSpec(type="integer", required=False, default=20, ge=1),
        "s": FieldSpec(type="string", required=False, default="x"),
        "d": FieldSpec(type="date", required=False, default="2026-10-06"),
        "z": FieldSpec(type="string", required=False, default=None),
    }
    model = build_model("D", spec)
    assert model.model_validate({}).model_dump()["n"] == 20


def test_an_optional_field_accepts_an_explicit_null_as_documented() -> None:
    model = build_model("O", {"x": FieldSpec(type="string", required=False)})
    assert model.model_validate({"x": None}).model_dump() == {"x": None}
    required = build_model("R", {"x": FieldSpec(type="string")})
    with pytest.raises(ValueError):
        required.model_validate({"x": None})  # required and not nullable: null is refused


def test_a_strict_target_object_rejects_fields_the_source_would_add() -> None:
    m = raw()
    tool(m, "erp_get_available_slots")["output"]["pagination"]["fields"]["extra"] = {
        "type": "string",
        "required": False,
    }
    capability(m, "scheduling.availability")["output"]["next_cursor"] = {
        "type": "object",
        "required": False,
        "fields": {"next_cursor": {"type": "string", "required": False}},
    }
    binding(m, "scheduling.availability")["output_map"]["next_cursor"] = "$.pagination"
    assert "MAPPING_TYPE_MISMATCH" in codes_of(
        m
    )  # the extra field would fail validation at runtime


# --- Phase 5.3: the compiler accepts exactly the mapping language the runtime executes ---


def test_a_constant_must_have_the_right_structure_not_just_the_right_category() -> None:
    m = raw()
    binding(m, "scheduling.availability")["output_map"]["slots"] = {"const": ["oops"]}
    assert "MAPPING_TYPE_MISMATCH" in codes_of(m)  # list[string] is not list[Slot]
    m = raw()
    binding(m, "scheduling.create")["output_map"]["status"] = {"const": {"x": "bad"}}
    assert "MAPPING_TYPE_MISMATCH" in codes_of(m)  # an object is not a string
    m = raw()
    binding(m, "scheduling.availability")["output_map"]["slots"] = {"const": []}
    assert compile_manifest(m).digest  # an empty list fits any list


def test_a_ref_default_must_fit_the_target_because_it_replaces_the_value() -> None:
    m = raw()
    binding(m, "scheduling.create")["output_map"]["status"] = {"from": "$.state", "default": 123}
    with pytest.raises(CompileError, match=r"integer \| string"):
        compile_manifest(m)
    binding(m, "scheduling.create")["output_map"]["status"] = {"from": "$.state", "default": "ok"}
    assert compile_manifest(m).digest


@pytest.mark.parametrize("source", ["$.pagination", "$.items"])
def test_an_enum_map_only_reads_strings(source: str) -> None:
    m = raw()
    binding(m, "scheduling.availability")["output_map"]["next_cursor"] = {
        "from": source,
        "enum": {"x": "y"},
    }
    assert "MAPPING_ENUM_SOURCE" in codes_of(m)  # the runtime raises on a non-string value


def test_a_wildcard_yields_a_list_exactly_like_the_runtime() -> None:
    m = raw()
    binding(m, "scheduling.availability")["output_map"]["next_cursor"] = "$.items[*].start"
    with pytest.raises(CompileError, match="list\\[string\\]"):
        compile_manifest(m)  # runtime would return ["...", "..."] for a string field
    binding(m, "scheduling.availability")["output_map"]["next_cursor"] = "$.items[0].start"
    assert compile_manifest(m).digest  # a single index is one string


def test_each_needs_a_list_of_objects() -> None:
    m = raw()
    tool(m, "erp_get_available_slots")["output"]["items"] = {
        "type": "list",
        "items": {"type": "string"},
    }
    assert "MAPPING_EACH_NEEDS_OBJECTS" in codes_of(m)


def test_each_cannot_fill_a_list_of_scalars() -> None:
    m = raw()
    capability(m, "scheduling.availability")["output"]["slots"] = {
        "type": "list",
        "items": {"type": "string"},
    }
    assert "MAPPING_TYPE_MISMATCH" in codes_of(m)


# --- Phase 5.4: nullability, unions, transport, recovery.result_map ---


def make_state_optional(m: dict[str, Any]) -> None:
    tool(m, "erp_create_reservation")["output"]["state"] = {"type": "string", "required": False}


def test_an_optional_tool_field_cannot_fill_a_required_capability_field() -> None:
    m = raw()
    make_state_optional(m)
    with pytest.raises(CompileError, match="string\\|null but the target takes string"):
        compile_manifest(m)  # a missing `state` would fail the mapping AFTER the write


def test_a_default_makes_a_nullable_source_safe() -> None:
    m = raw()
    make_state_optional(m)
    binding(m, "scheduling.create")["output_map"]["status"] = {"from": "$.state", "default": "n/a"}
    assert compile_manifest(m).digest  # absent or null falls back to "n/a"


def test_null_only_fits_a_nullable_target() -> None:
    m = raw()
    binding(m, "scheduling.create")["output_map"]["status"] = {"const": None}
    assert "MAPPING_TYPE_MISMATCH" in codes_of(m)
    m = raw()
    binding(m, "scheduling.availability")["output_map"]["next_cursor"] = {"const": None}
    assert compile_manifest(m).digest  # next_cursor is nullable


def test_values_of_different_types_are_a_union_not_anything() -> None:
    m = raw()
    binding(m, "scheduling.availability")["input_map"]["service_code"]["enum"]["consultation"] = 123
    with pytest.raises(CompileError, match="integer \\| string"):
        compile_manifest(m)


def test_a_transform_or_enum_map_cannot_read_a_nullable_source_without_a_default() -> None:
    m = raw()
    capability(m, "scheduling.create")["input"]["duration_minutes"] = {
        "type": "integer",
        "required": False,
    }
    with pytest.raises(CompileError, match="can be null"):
        compile_manifest(m)
    binding(m, "scheduling.create")["input_map"]["hours"]["default"] = 0.5
    assert compile_manifest(m).digest


def test_a_path_through_an_optional_object_may_be_absent() -> None:
    m = raw()
    tool(m, "erp_get_available_slots")["output"]["pagination"]["required"] = False
    capability(m, "scheduling.availability")["output"]["next_cursor"] = {"type": "string"}
    assert "MAPPING_TYPE_MISMATCH" in codes_of(m)  # pagination may be missing -> null


def test_the_runtime_falls_back_to_the_default_for_null_as_the_compiler_assumes() -> None:
    from conversation_agent.core.definitions.mapping import Ref
    from conversation_agent.tools.mapping import apply_mapping

    spec = {"x": Ref.model_validate({"from": "$.a", "default": "d"})}
    assert apply_mapping(spec, {"a": None}) == {"x": "d"}
    assert apply_mapping(spec, {}) == {"x": "d"}
    assert apply_mapping(spec, {"a": "v"}) == {"x": "v"}


def test_the_http_spec_must_send_every_required_tool_argument() -> None:
    m = raw()
    tool(m, "erp_create_reservation")["http"]["body"] = ["service_code", "starts_at"]  # no hours
    with pytest.raises(CompileError, match=r"required input .hours."):
        compile_manifest(m)


def test_the_http_spec_cannot_reference_arguments_the_tool_does_not_have() -> None:
    m = raw()
    tool(m, "erp_find_reservation")["http"]["path"] = "/bookings/by-idempotency-key/{missing}"
    tool(m, "erp_get_available_slots")["http"]["query"].append("nonexistent")
    with pytest.raises(CompileError) as caught:
        compile_manifest(m)
    assert {"HTTP_UNKNOWN_ARGUMENT", "TOOL_ARG_NOT_SENT"} <= caught.value.codes


def lookup_with_result_map(m: dict[str, Any], result_map: dict[str, Any]) -> None:
    tool(m, "erp_create_reservation")["recovery"]["result_map"] = result_map


def test_a_valid_recovery_result_map_compiles() -> None:
    m = raw()
    lookup_with_result_map(m, {"booking_id": "$.booking_id", "status": "$.status"})
    assert compile_manifest(m).digest


def test_recovery_result_map_is_checked_like_a_binding() -> None:
    m = raw()
    lookup_with_result_map(m, {"booking_id": "$.does_not_exist", "status": {"const": 123}})
    with pytest.raises(CompileError) as caught:
        compile_manifest(m)
    assert {"MAPPING_UNKNOWN_SOURCE", "MAPPING_TYPE_MISMATCH"} <= caught.value.codes
    lookup_with_result_map(m, {"booking_id": "$.booking_id"})  # `status` is required
    assert "MAPPING_MISSING_REQUIRED" in codes_of(m)


def test_a_required_field_with_a_default_rejects_an_explicit_null() -> None:
    model = build_model("P", {"x": FieldSpec(type="string", required=False, default="abc")})
    assert model.model_validate({}).model_dump() == {"x": "abc"}
    assert model.model_validate({"x": "foo"}).model_dump() == {"x": "foo"}
    with pytest.raises(ValueError):
        model.model_validate({"x": None})
