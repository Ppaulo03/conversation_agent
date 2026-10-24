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
    if isinstance(value, dict) and "media_id" in value:  # a file the contact sent
        return str(value.get("filename") or f"{value.get('kind', 'file')} ({value.get('handle')})")
    if isinstance(value, list):  # one item per line: the person reads all that will be written
        return "\n".join(f"- {_item(item, timezone)}" for item in value)
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


def _item(item: Any, timezone: str | None) -> str:
    """A list item on one line: an object as 'key: value, ...' (empty fields left out), a list as '(a, b)'."""
    if isinstance(item, dict) and "media_id" not in item:
        parts = [f"{k}: {_item(v, timezone)}" for k, v in item.items() if v not in (None, "", [])]
        return ", ".join(parts)
    if isinstance(item, list):
        return "(" + ", ".join(_item(i, timezone) for i in item) + ")"
    return display_value(item, timezone)


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
