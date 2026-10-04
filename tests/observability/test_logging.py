"""Structured logging and the shared observability context (Phase 11)."""

from __future__ import annotations

import asyncio
import io
import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest

from conversation_agent.adapters.observability.logs import (
    MAX_TEXT,
    JsonFormatter,
    configure_logging,
)
from conversation_agent.core.observability import (
    FIELDS,
    bind,
    conversation_ref,
    current,
)


@pytest.fixture
def stream() -> Iterator[io.StringIO]:
    out = io.StringIO()
    handler = configure_logging(logging.DEBUG, stream=out)
    yield out
    logging.getLogger("conversation_agent").removeHandler(handler)
    logging.getLogger("conversation_agent").propagate = True


def lines(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line]


log = logging.getLogger("conversation_agent.tests")


# --- the context ---


def test_bound_fields_nest_and_are_restored() -> None:
    assert current() == {}
    with bind(tenant_id="t1", component="x"):
        with bind(turn_id="turn-1", component="y"):
            assert current() == {"tenant_id": "t1", "turn_id": "turn-1", "component": "y"}
        assert current() == {"tenant_id": "t1", "component": "x"}
    assert current() == {}


def test_a_none_value_does_not_erase_what_is_bound_and_an_unknown_field_is_refused() -> None:
    with bind(tenant_id="t1"), bind(tenant_id=None, turn_id="turn-1"):
        assert current() == {"tenant_id": "t1", "turn_id": "turn-1"}
    with (
        pytest.raises(ValueError, match="unknown observability field"),
        bind(message_text="hello"),  # the schema is closed: no content-carrying field
    ):
        pass
    assert not {"text", "message", "content", "args"} & FIELDS


async def test_concurrent_tasks_do_not_see_each_others_context() -> None:
    seen: dict[str, str | None] = {}

    async def work(name: str) -> None:
        with bind(tenant_id=name):
            await asyncio.sleep(0.01)  # let the others run while this one is bound
            seen[name] = current().get("tenant_id")

    await asyncio.gather(*(work(f"t{i}") for i in range(10)))
    assert seen == {f"t{i}": f"t{i}" for i in range(10)}
    assert current() == {}


def test_a_conversation_is_named_by_a_stable_reference_never_by_its_id() -> None:
    ref = conversation_ref("t1", "inst_1:5511999990000")
    assert ref == conversation_ref("t1", "inst_1:5511999990000") and len(ref) == 16
    assert "5511999990000" not in ref and ref != conversation_ref("t2", "inst_1:5511999990000")


# --- the JSON line ---


def test_a_line_is_one_json_object_with_the_envelope_the_context_and_the_fields(
    stream: io.StringIO,
) -> None:
    with bind(tenant_id="t1", turn_id="turn-9", component="coordinator"):
        log.warning("outbox.send_not_confirmed", extra={"fields": {"attempts": 3, "ok": False}})
    (line,) = lines(stream)
    assert line["event"] == "outbox.send_not_confirmed" and line["level"] == "warning"
    assert line["logger"] == "conversation_agent.tests" and line["ts"].endswith("+00:00")
    assert (line["tenant_id"], line["turn_id"], line["component"]) == (
        "t1",
        "turn-9",
        "coordinator",
    )
    assert line["attempts"] == 3 and line["ok"] is False


def test_personal_data_is_scrubbed_and_text_is_capped(stream: io.StringIO) -> None:
    log.info(
        "x",
        extra={
            "fields": {
                "reason": "contact ana@example.com called +55 11 99999-0000",
                "long": "y" * 5000,
                "nested": {"cpf": "123.456.789-09"},
                "items": ["bia@example.com", 3],
            }
        },
    )
    (line,) = lines(stream)
    assert "ana@example.com" not in line["reason"] and "[REDACTED_EMAIL]" in line["reason"]
    assert "99999-0000" not in line["reason"]
    assert len(line["long"]) == MAX_TEXT
    assert line["nested"]["cpf"] == "[REDACTED_CPF]" and line["items"] == ["[REDACTED_EMAIL]", 3]


def test_the_event_itself_is_scrubbed_too(stream: io.StringIO) -> None:
    log.info("user ana@example.com wrote")
    assert "ana@example.com" not in lines(stream)[0]["event"]


def test_a_call_cannot_overwrite_the_bound_context(stream: io.StringIO) -> None:
    with bind(tenant_id="real"):
        log.info("x", extra={"fields": {"tenant_id": "forged", "event": "forged"}})
    (line,) = lines(stream)
    assert line["tenant_id"] == "real" and line["event"] == "x"


def test_an_exception_is_its_type_and_a_scrubbed_message_never_a_stack_by_default(
    stream: io.StringIO,
) -> None:
    try:
        raise RuntimeError("boom for ana@example.com")
    except RuntimeError:
        log.exception("failed")
    (line,) = lines(stream)
    assert line["exc_type"] == "RuntimeError" and "ana@example.com" not in line["exc_message"]
    assert "stack" not in line
    record = logging.LogRecord("n", logging.ERROR, "f", 1, "m", None, None)
    try:
        raise ValueError("v")
    except ValueError:
        import sys

        record.exc_info = sys.exc_info()
    assert "Traceback" in json.loads(JsonFormatter(include_stack=True).format(record))["stack"]


def test_values_that_are_not_json_do_not_break_the_line(stream: io.StringIO) -> None:
    log.info("x", extra={"fields": {"obj": object(), "multi": "a\nb"}})
    (line,) = lines(stream)
    assert "object" in line["obj"] and line["multi"] == "a\nb"
    assert stream.getvalue().count("\n") == 1  # newlines are escaped: one line per record


def test_configuring_twice_installs_one_handler() -> None:
    first = configure_logging(stream=io.StringIO())
    second = configure_logging(stream=io.StringIO())
    ours = [
        h
        for h in logging.getLogger("conversation_agent").handlers
        if getattr(h, "_conversation_agent_json", False)
    ]
    assert ours == [second] and first not in ours
    logging.getLogger("conversation_agent").removeHandler(second)
    logging.getLogger("conversation_agent").propagate = True
