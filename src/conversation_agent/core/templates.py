"""Text templates used by the runtime (`{name}` placeholders), checked when an agent is defined.

A template that cannot be formatted must never be discovered in a conversation: unknown names,
attribute/index access (`{a.b}`, `{a[0]}`), positional fields and unbalanced braces are all
definition errors.
"""

from __future__ import annotations

import string

from conversation_agent.core.errors import DefinitionError


def placeholders(text: str) -> set[str]:
    """The field names a template uses; raises DefinitionError when it is not well formed."""
    try:
        parsed = list(string.Formatter().parse(text))
    except ValueError as exc:
        raise DefinitionError(f"malformed template {text!r}: {exc}") from exc
    names: set[str] = set()
    for _, field, spec, conversion in parsed:
        if field is None:
            continue
        if not field.isidentifier():
            raise DefinitionError(
                f"template {text!r}: {{{field}}} must be a plain name (no positions, attributes "
                "or indexes)"
            )
        if spec or conversion:
            raise DefinitionError(f"template {text!r}: {{{field}}} cannot carry a format spec")
        names.add(field)
    return names


def check_template(
    where: str,
    text: str,
    allowed: set[str] | frozenset[str],
    *,
    required: frozenset[str] = frozenset(),
) -> None:
    used = placeholders(text)
    unknown = used - set(allowed)
    if unknown:
        raise DefinitionError(
            f"{where}: unknown placeholder(s) {sorted(unknown)}; allowed: {sorted(allowed)}"
        )
    missing = required - used
    if missing:
        raise DefinitionError(f"{where}: must contain {sorted(missing)}")
