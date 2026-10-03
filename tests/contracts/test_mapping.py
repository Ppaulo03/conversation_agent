"""Mapping language tests: select/rename, enum, transform, const, default, list mapping."""

from __future__ import annotations

from typing import Any

import pytest

from conversation_agent.core.definitions.mapping import Const, Each, Ref
from conversation_agent.core.errors import MappingError
from conversation_agent.tools.mapping import apply_mapping

API_RESPONSE: dict[str, Any] = {
    "items": [
        {"start": "2026-10-06T09:00:00-03:00", "end": "2026-10-06T09:30:00-03:00"},
        {"start": "2026-10-06T09:30:00-03:00", "end": "2026-10-06T10:00:00-03:00"},
    ],
    "pagination": {"next_cursor": "20"},
    "meta": {"tags": ["a", "b"]},
}


def test_select_and_rename_nested_paths() -> None:
    out = apply_mapping(
        {"cursor": "$.pagination.next_cursor", "first_tag": "$.meta.tags[0]"}, API_RESPONSE
    )
    assert out == {"cursor": "20", "first_tag": "a"}


def test_list_mapping_from_each() -> None:
    spec = {
        "slots": Each(from_each="$.items[*]", map={"start_at": "$.start", "end_at": "$.end"}),
        "next_cursor": "$.pagination.next_cursor",
    }
    out = apply_mapping(spec, API_RESPONSE)
    assert out["slots"] == [
        {"start_at": "2026-10-06T09:00:00-03:00", "end_at": "2026-10-06T09:30:00-03:00"},
        {"start_at": "2026-10-06T09:30:00-03:00", "end_at": "2026-10-06T10:00:00-03:00"},
    ]
    assert out["next_cursor"] == "20"


def test_empty_list_maps_to_empty_list() -> None:
    out = apply_mapping(
        {"slots": Each(from_each="$.items[*]", map={"s": "$.start"})}, {"items": []}
    )
    assert out == {"slots": []}


def test_enum_mapping_and_unknown_enum_value_is_an_error() -> None:
    spec = {"code": Ref(from_="$.service", enum={"haircut": "HC-01"})}
    assert apply_mapping(spec, {"service": "haircut"}) == {"code": "HC-01"}
    with pytest.raises(MappingError):
        apply_mapping(spec, {"service": "massage"})


def test_unit_conversion_transforms() -> None:
    assert apply_mapping({"h": Ref(from_="$.m", transform="minutes_to_hours")}, {"m": 90}) == {
        "h": 1.5
    }
    assert apply_mapping({"m": Ref(from_="$.h", transform="hours_to_minutes")}, {"h": 0.5}) == {
        "m": 30
    }


def test_datetime_normalisation_to_utc() -> None:
    out = apply_mapping(
        {"t": Ref(from_="$.t", transform="datetime_to_utc")}, {"t": "2026-10-06T10:00:00-03:00"}
    )
    assert out == {"t": "2026-10-06T13:00:00+00:00"}


def test_constant_and_default() -> None:
    spec = {"limit": Const(const=20), "opt": Ref(from_="$.missing", default=None)}
    assert apply_mapping(spec, {}) == {"limit": 20, "opt": None}


def test_missing_path_without_default_is_an_error() -> None:
    with pytest.raises(MappingError):
        apply_mapping({"x": "$.nope"}, {})


def test_unknown_transform_is_an_error() -> None:
    with pytest.raises(MappingError):
        apply_mapping({"x": Ref(from_="$.a", transform="rm_rf")}, {"a": 1})


def test_failing_transform_is_a_mapping_error() -> None:
    with pytest.raises(MappingError):
        apply_mapping({"x": Ref(from_="$.a", transform="datetime_to_utc")}, {"a": "not a date"})


@pytest.mark.parametrize("path", ["items", "$.items[", "$..x", "$.a b"])
def test_malformed_paths_are_rejected(path: str) -> None:
    with pytest.raises(MappingError):
        apply_mapping({"x": path}, {"items": []})


def test_mapping_accepts_declarative_dict_form() -> None:
    """The spec is serialisable-shaped: dicts validate into the same expressions (YAML later)."""
    from conversation_agent.core.definitions.binding import CapabilityBinding

    binding = CapabilityBinding.model_validate(
        {
            "capability": "c",
            "tool": "t",
            "input_map": {"a": {"from": "$.x", "transform": "minutes_to_hours"}, "b": {"const": 1}},
            "output_map": {"l": {"from_each": "$.i[*]", "map": {"s": "$.v"}}},
        }
    )
    assert apply_mapping(binding.input_map, {"x": 60}) == {"a": 1.0, "b": 1}
    assert apply_mapping(binding.output_map, {"i": [{"v": 1}]}) == {"l": [{"s": 1}]}
