"""Deterministic canonicalisation and hashing (DESIGN §9.2).

Used for `args_hash`, journal `request_hash` and stable identities. Datetimes are
normalised to UTC, decimals to a canonical string, keys are sorted.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

type JSON = dict[str, Any] | list[Any] | str | int | float | bool | None


def canonicalize(value: Any) -> JSON:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("naive datetime cannot be canonicalised; timezone is required")
        return value.astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, Enum):
        return canonicalize(value.value)
    if isinstance(value, Mapping):
        return {
            str(k): canonicalize(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))
        }
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return [canonicalize(v) for v in value]
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise TypeError(f"cannot canonicalise value of type {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(
        canonicalize(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def stable_hash(*parts: Any) -> str:
    return hashlib.sha256(canonical_json(list(parts)).encode("utf-8")).hexdigest()
