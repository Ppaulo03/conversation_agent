"""Canonical error model tests (DESIGN §6.3/§23.4; INV-006, INV-021)."""

from __future__ import annotations

import pytest

from conversation_agent.core.definitions.binding import ErrorMap, ErrorRule
from conversation_agent.tools.error_mapping import (
    classify_http_status,
    classify_invalid_response,
    classify_timeout,
)

EMPTY = ErrorMap()


def test_2xx_is_not_a_failure() -> None:
    assert classify_http_status(200, EMPTY, "read") is None
    assert classify_http_status(204, EMPTY, "write") is None


def test_error_map_http_status_specialises_business_errors() -> None:
    error_map = ErrorMap(
        http_status={409: ErrorRule(type="business_error", code="SLOT_UNAVAILABLE")}
    )
    result = classify_http_status(409, error_map, "irreversible")
    assert result is not None
    assert result.status == "business_error"
    assert result.error is not None and result.error.code == "SLOT_UNAVAILABLE"
    assert not result.error.retryable


@pytest.mark.parametrize("status", [400, 422])
def test_default_validation_errors(status: int) -> None:
    result = classify_http_status(status, EMPTY, "read")
    assert result is not None and result.status == "validation_error"


def test_429_is_retryable_technical_error() -> None:
    result = classify_http_status(429, EMPTY, "write")
    assert result is not None and result.status == "technical_error"
    assert result.error is not None and result.error.retryable


def test_read_5xx_defaults_to_retryable_technical_error() -> None:
    result = classify_http_status(503, EMPTY, "read")
    assert result is not None and result.status == "technical_error"
    assert result.error is not None and result.error.retryable


@pytest.mark.parametrize("risk", ["write", "irreversible"])
def test_write_5xx_defaults_to_unknown(risk: str) -> None:
    result = classify_http_status(502, EMPTY, risk)  # type: ignore[arg-type]
    assert result is not None and result.status == "unknown"


@pytest.mark.parametrize("risk", ["write", "irreversible"])
def test_error_map_cannot_downgrade_ambiguous_write_5xx(risk: str) -> None:
    """A binding that *claims* technical_error for a write 5xx is overridden: stays unknown."""
    sneaky = ErrorMap(
        default_5xx={"write": ErrorRule(type="technical_error", retryable=True)},
        http_status={500: ErrorRule(type="technical_error", retryable=True)},
    )
    for status in (500, 503):
        result = classify_http_status(status, sneaky, risk)  # type: ignore[arg-type]
        assert result is not None and result.status == "unknown"
        assert result.error is not None and not result.error.retryable


def test_read_timeout_default_and_write_timeout_unknown() -> None:
    read = classify_timeout(EMPTY, "read", request_sent=True)
    assert read.status == "timeout" and read.error is not None and read.error.retryable
    write = classify_timeout(EMPTY, "write", request_sent=True)
    assert write.status == "unknown" and write.error is not None and not write.error.retryable


def test_error_map_cannot_downgrade_ambiguous_write_timeout() -> None:
    sneaky = ErrorMap(timeout={"write": ErrorRule(type="timeout", retryable=True)})
    assert classify_timeout(sneaky, "irreversible", request_sent=True).status == "unknown"


def test_connect_failure_never_sent_is_safe_even_for_writes() -> None:
    result = classify_timeout(EMPTY, "irreversible", request_sent=False)
    assert result.status == "technical_error"
    assert result.error is not None and result.error.retryable


def test_invalid_2xx_body_is_unknown_for_writes() -> None:
    assert classify_invalid_response("read").status == "technical_error"
    assert classify_invalid_response("write").status == "unknown"


def test_messages_are_safe_and_generic() -> None:
    result = classify_http_status(500, EMPTY, "read")
    assert result is not None and result.error is not None
    assert "Traceback" not in result.error.message_safe
