"""Redaction of secrets and common PII before anything reaches logs or traces (DESIGN §11.1)."""

from __future__ import annotations

import re

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer [REDACTED]"),
    (re.compile(r"\b(?:sk|gsk|pk|rk|xox[abp])[-_][A-Za-z0-9_-]{10,}"), "[REDACTED_KEY]"),
    (re.compile(r"(?i)\b(api[_-]?key|token|secret|password)\s*[=:]\s*\S+"), r"\1=[REDACTED]"),
    (re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b"), "[REDACTED_EMAIL]"),
    (re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b"), "[REDACTED_CPF]"),
    (re.compile(r"\b\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}\b"), "[REDACTED_CNPJ]"),
    (re.compile(r"\+?\d[\d\s().-]{9,}\d"), "[REDACTED_PHONE]"),
)


def redact(text: str) -> str:
    """Best-effort masking; call it on anything free-form before logging."""
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text
