"""Words for what happened, built from data and never from a model.

A confirmed action that succeeded is reported with the capability's `executed_template`: its
`{name}` placeholders come from the call's arguments and the result's data (the result wins), with
datetimes shown in the agent's timezone ("ter 06/10 às 10:00") instead of the canonical UTC form.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from conversation_agent.core.temporal_ptbr import format_local, format_moment


def display_value(value: Any, timezone: str | None) -> str:
    """People's words for a value. A datetime with an offset becomes 'ter 06/10 às 10:00' in the
    agent's timezone, or, with no timezone given, in the offset it carries (as the person said)."""
    if isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value)
        except ValueError:
            return value
        if moment.tzinfo is not None:
            day, hour = format_local(moment, timezone) if timezone else format_moment(moment)
            return f"{day} às {hour}"
        return value
    return str(value)


def render_executed(
    template: str, args: dict[str, Any], data: dict[str, Any] | None, timezone: str
) -> str | None:
    """The sentence, or None when the result lacks a field the template needs (the caller then
    falls back rather than say something half-true)."""
    values = {**args, **(data or {})}
    shown = {name: display_value(value, timezone) for name, value in values.items()}
    try:
        return template.format_map(shown)
    except (KeyError, IndexError, ValueError):
        return None
