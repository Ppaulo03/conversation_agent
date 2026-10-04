"""Structured JSON logging that carries the observability context and never carries PII.

Every record becomes ONE JSON object per line with a fixed envelope (`ts`, `level`, `logger`,
`event`) plus the bound context (`tenant_id`, `turn_id`, ...) plus the call's own `fields`. The
call site gives a short stable EVENT NAME and puts the variable parts in `fields`:

    log.info("outbox.delivery_unknown", extra={"fields": {"attempts": 3}})

Free text from the outside world is scrubbed (`core.redaction`) and capped; an exception is
reported as its type and a redacted message (the stack is off by default: frames can hold data).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from conversation_agent.core.observability import current
from conversation_agent.core.redaction import redact

MAX_TEXT = 500
_RESERVED = set(vars(logging.makeLogRecord({}))) | {"message", "asctime", "fields"}


def _safe(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)[:MAX_TEXT]
    if isinstance(value, bool) or value is None or isinstance(value, int | float):
        return value
    if isinstance(value, list | tuple):
        return [_safe(v) for v in list(value)[:20]]
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in list(value.items())[:20]}
    return redact(str(value))[:MAX_TEXT]


class JsonFormatter(logging.Formatter):
    def __init__(self, *, include_stack: bool = False) -> None:
        super().__init__()
        self._stack = include_stack

    def format(self, record: logging.LogRecord) -> str:
        body: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "event": redact(record.getMessage())[:MAX_TEXT],
        }
        body.update(current())
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            body.update({k: _safe(v) for k, v in fields.items() if k not in body})
        if record.exc_info and record.exc_info[0] is not None:
            body["exc_type"] = record.exc_info[0].__name__
            body["exc_message"] = redact(str(record.exc_info[1]))[:MAX_TEXT]
            if self._stack:
                body["stack"] = redact(self.formatException(record.exc_info))[:4000]
        return json.dumps(body, ensure_ascii=False, separators=(",", ":"))


def configure_logging(
    level: int | str = logging.INFO, *, include_stack: bool = False, stream: Any = None
) -> logging.Handler:
    """Installs ONE JSON handler on the `conversation_agent` logger (idempotent) and returns it."""
    logger = logging.getLogger("conversation_agent")
    for existing in list(logger.handlers):
        if getattr(existing, "_conversation_agent_json", False):
            logger.removeHandler(existing)
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter(include_stack=include_stack))
    handler._conversation_agent_json = True  # type: ignore[attr-defined]
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    return handler
