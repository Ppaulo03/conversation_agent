"""Canonical error classification for HTTP-shaped outcomes (DESIGN §6.3, §23.4).

Conservative rule (INV-006/INV-021): when a non-read operation may have reached the
external system and the outcome is unknown (5xx, timeout, broken connection), the result
is `unknown`, whatever the binding's `error_map` says. `technical_error` is reserved for
cases where non-execution is known or repeating is safe by contract.
"""

from __future__ import annotations

from typing import Literal

from conversation_agent.core.definitions.binding import ErrorMap, ErrorRule
from conversation_agent.core.definitions.capability import Risk
from conversation_agent.core.models.tooling import ToolError, ToolResult

Bucket = Literal["read", "write"]


def _bucket(risk: Risk) -> Bucket:
    return "read" if risk == "read" else "write"


def _failure(rule: ErrorRule, code: str, message: str, meta: dict[str, object]) -> ToolResult:
    return ToolResult(
        status=rule.type,
        error=ToolError(
            code=rule.code or code,
            message_safe=message,
            retryable=rule.retryable and rule.type in ("technical_error", "timeout"),
        ),
        provider_metadata=dict(meta),
    )


def _coerce_ambiguous(rule: ErrorRule, risk: Risk) -> ErrorRule:
    """A possibly-delivered non-read call can never be reported as safely retryable."""
    if risk != "read" and rule.type in ("technical_error", "timeout"):
        return ErrorRule(type="unknown", code=rule.code, retryable=False)
    return rule


def classify_http_status(status: int, error_map: ErrorMap, risk: Risk) -> ToolResult | None:
    """Returns None for 2xx (caller handles success); a canonical failure otherwise."""
    meta: dict[str, object] = {"http_status": status}
    if 200 <= status < 300:
        return None
    if status in error_map.http_status:
        rule = error_map.http_status[status]
        # A mapped 5xx is still ambiguous for writes.
        if status >= 500:
            rule = _coerce_ambiguous(rule, risk)
        return _failure(rule, f"HTTP_{status}", f"External system answered HTTP {status}.", meta)
    if status >= 500:
        rule = error_map.default_5xx.get(_bucket(risk)) or (
            ErrorRule(type="technical_error", retryable=True)
            if risk == "read"
            else ErrorRule(type="unknown")
        )
        return _failure(
            _coerce_ambiguous(rule, risk),
            "EXTERNAL_5XX",
            f"External system failed (HTTP {status}).",
            meta,
        )
    if status in (400, 422):
        return _failure(
            ErrorRule(type="validation_error"),
            "EXTERNAL_REJECTED_REQUEST",
            f"External system rejected the request (HTTP {status}).",
            meta,
        )
    if status == 429:
        return _failure(
            ErrorRule(type="technical_error", retryable=True),
            "EXTERNAL_RATE_LIMITED",
            "External system is rate limiting requests.",
            meta,
        )
    # Unmapped 3xx/4xx: the server answered and did not execute; nothing to retry blindly.
    return _failure(
        ErrorRule(type="technical_error"),
        "EXTERNAL_UNEXPECTED_STATUS",
        f"External system answered an unexpected HTTP {status}.",
        meta,
    )


def classify_timeout(error_map: ErrorMap, risk: Risk, *, request_sent: bool) -> ToolResult:
    """Timeout / broken connection. `request_sent=False` (connect failed): nothing executed."""
    meta: dict[str, object] = {"request_sent": request_sent}
    if not request_sent:
        return _failure(
            ErrorRule(type="technical_error", retryable=True),
            "EXTERNAL_UNREACHABLE",
            "Could not reach the external system; the request was not sent.",
            meta,
        )
    rule = error_map.timeout.get(_bucket(risk)) or (
        ErrorRule(type="timeout", retryable=True) if risk == "read" else ErrorRule(type="unknown")
    )
    return _failure(
        _coerce_ambiguous(rule, risk),
        "EXTERNAL_TIMEOUT",
        "The external system did not answer in time; the outcome is not known.",
        meta,
    )


def classify_invalid_response(risk: Risk) -> ToolResult:
    """2xx with an unusable body: a write may have happened -> unknown."""
    rule = ErrorRule(type="technical_error") if risk == "read" else ErrorRule(type="unknown")
    return _failure(
        rule,
        "EXTERNAL_INVALID_RESPONSE",
        "The external system returned an unusable response.",
        {},
    )
