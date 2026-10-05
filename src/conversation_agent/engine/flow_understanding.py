"""Deterministic understanding for flows (DESIGN §22): the first, cheapest, safest pass.

Everything here is a pure function of (flow definition, text, today). Nothing calls a model or
reads a clock. A value is only produced when it is unambiguous; otherwise nothing is, and the
flow asks (or, later, the journaled LLM extractor proposes a raw span that is validated here).
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from conversation_agent.core.definitions.flow import FlowDefinition, SlotDefinition
from conversation_agent.core.temporal_ptbr import (
    fold,
    format_local,
    parse_date,
    parse_duration,
    parse_time,
)
from conversation_agent.engine.confirmation import interpret_by_rules

_CANCEL = re.compile(
    r"\b(cancela|cancelar|cancele|deixa pra la|deixa para la|esquece|esqueca|desisto|desistir|"
    r"nao quero mais|melhor nao)\b"
)
_ORDINALS = {
    "primeiro": 1, "primeira": 1, "segundo": 2, "terceiro": 3, "terceira": 3,
    "quarto": 4, "quinto": 5,
}  # fmt: skip
# feminine segunda/quarta/quinta are weekdays: never read as an ordinal
_MAX_CANCEL_WORDS = 6  # "cancela" inside a long sentence is more likely a different request


def is_cancel(text: str) -> bool:
    folded = fold(text)
    return len(folded.split()) <= _MAX_CANCEL_WORDS and _CANCEL.search(folded) is not None


_QUESTION_WORDS = frozenset(
    {"como", "qual", "quais", "quanto", "quantos", "quanta", "quantas", "onde", "quando", "quem",
     "porque", "voces", "tem", "posso", "pode", "podem", "existe"}
)  # fmt: skip
_NEGATIONS = frozenset({"nao", "nunca", "jamais", "sem"})


def looks_like_question(text: str) -> bool:
    """A conservative sniff: a question mark, or an opening interrogative word. Used only to decide
    when a free-text slot should NOT trust the rules (the model then decides)."""
    stripped = text.strip()
    if "?" in stripped:
        return True
    words = fold(stripped).split()
    return bool(words) and (
        words[0] in _QUESTION_WORDS or " ".join(words[:2]) in {"por que", "o que"}
    )


def matches_request(text: str, triggers: tuple[str, ...], max_words: int) -> bool:
    """A SHORT message that asks for what a trigger phrase names, and does not negate it.
    "quero falar com um atendente" matches; "nao quero falar com atendente" and a paragraph that
    merely mentions an attendant do not."""
    folded = fold(text)
    words = folded.split()
    if not words or len(words) > max_words:
        return False
    padded = f" {folded} "
    for phrase in triggers:
        needle = f" {fold(phrase)} "
        at = padded.find(needle)
        if at < 0:
            continue
        before = padded[:at].split()[-3:]  # the three words leading into the phrase
        if not _NEGATIONS & set(before):
            return True
    return False


def phrase_score(text: str, phrases: tuple[str, ...]) -> int:
    """Words in the longest trigger phrase found in `text` (0 = none): more words, more specific."""
    folded = f" {fold(text)} "
    return max((len(fold(p).split()) for p in phrases if f" {fold(p)} " in folded), default=0)


# --- slots ---


def _enum_value(slot: SlotDefinition, folded: str) -> str | None:
    """The one choice the text names. When one term sits inside a longer one that also matched
    ("tennis" inside "beach tennis"), the longer wins: the shorter was only part of it."""
    hits: list[tuple[str, int, int]] = []  # (canonical, start, end) of every matched term
    for canonical, synonyms in slot.choices.items():
        for term in (canonical, *synonyms):
            for found in re.finditer(rf"\b{re.escape(fold(term))}\b", folded):
                hits.append((canonical, found.start(), found.end()))
    standing = {
        canonical
        for canonical, start, end in hits
        if not any(
            other != canonical
            and o_start <= start
            and end <= o_end
            and (o_end - o_start) > (end - start)
            for other, o_start, o_end in hits
        )
    }
    return next(iter(standing)) if len(standing) == 1 else None  # two answers: ambiguous


def extract_slots(
    flow: FlowDefinition, text: str, today: date, awaiting: str | None
) -> dict[str, Any]:
    """Slot values stated in `text`. Free-form types (duration/text) are only read for the slot
    being asked, so '2h' is never guessed to be a duration when it was a time."""
    folded = fold(text)
    found: dict[str, Any] = {}
    by_type: dict[str, list[SlotDefinition]] = {}
    for definition in flow.slots:
        by_type.setdefault(definition.type, []).append(definition)

    for enum_slot in by_type.get("enum", []):
        value = _enum_value(enum_slot, folded)
        if value is not None:
            found[enum_slot.name] = value

    for kind, parse in (("date", _date_value), ("time", _time_value)):
        slots = by_type.get(kind, [])
        target = _target(slots, awaiting)
        if target is not None:
            value = parse(text, today)
            if value is not None:
                found[target.name] = value

    if awaiting is not None:
        asked = next((s for s in flow.slots if s.name == awaiting), None)
        if asked is not None and asked.type == "duration_minutes":
            minutes = parse_duration(text)
            if minutes is not None:
                found[asked.name] = minutes
        elif asked is not None and asked.type == "text" and text.strip():
            found[asked.name] = text.strip()[:200]
    return found


def _target(slots: list[SlotDefinition], awaiting: str | None) -> SlotDefinition | None:
    if len(slots) == 1:
        return slots[0]
    return next((s for s in slots if s.name == awaiting), None)  # several: only the one asked


def _date_value(text: str, today: date) -> str | None:
    parsed = parse_date(text, today)
    return parsed.isoformat() if parsed is not None else None


def _time_value(text: str, _today: date) -> str | None:
    parsed = parse_time(text)
    return parsed.strftime("%H:%M") if parsed is not None else None


# --- choosing among real options ---
def build_options(
    items: list[dict[str, Any]], value_field: str, timezone: str
) -> list[dict[str, Any]]:
    """Numbered options in the agent's timezone. The value is exactly what the tool returned."""
    options: list[dict[str, Any]] = []
    for item in items:
        value = item.get(value_field)
        label, local_date, local_time = str(value), None, None
        if isinstance(value, str):
            try:
                moment = datetime.fromisoformat(value)
            except ValueError:
                moment = None
            if moment is not None and moment.tzinfo is not None:
                day, hour = format_local(moment, timezone)
                label = f"{day} às {hour}"
                local = moment.astimezone(ZoneInfo(timezone))
                local_date, local_time = local.date().isoformat(), hour
        options.append(
            {"value": value, "label": label, "local_date": local_date, "local_time": local_time}
        )
    return options


def render_options(options: list[dict[str, Any]]) -> str:
    return "\n".join(f"{i}) {o['label']}" for i, o in enumerate(options, start=1))


def select_option(
    text: str,
    options: list[dict[str, Any]],
    today: date,
    *,
    unlisted: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """The one option the user means, or None. Never picks on a guess. A number or an ordinal
    refers to what was SHOWN; a stated time or date may also select a real option that did not
    fit in the message (`unlisted`)."""
    if not options:
        return None
    folded = fold(text)
    number = re.fullmatch(r"(?:(?:a|o) )?(?:(?:opcao|numero|n) )?(\d)", folded)
    if number is not None and 1 <= int(number[1]) <= len(options):
        return options[int(number[1]) - 1]
    ordinal = re.search(r"\b(primeir[oa]|segundo|terceir[oa]|quarto|quinto)\b", folded)
    if ordinal is not None and _ORDINALS[ordinal[1]] <= len(options):
        return options[_ORDINALS[ordinal[1]] - 1]
    if re.search(r"\b(ultim[oa])\b", folded):
        return options[-1]
    if len(options) == 1 and interpret_by_rules(text) == "confirm":
        return options[0]

    wanted_time = _time_value(text, today)
    parsed_day = parse_date(text, today)
    wanted_date = parsed_day.isoformat() if parsed_day is not None else None
    if wanted_time is None and wanted_date is None:
        return None
    matches = [
        o
        for o in [*options, *(unlisted or [])]
        if (wanted_time is None or o["local_time"] == wanted_time)
        and (wanted_date is None or o["local_date"] == wanted_date)
    ]
    return matches[0] if len(matches) == 1 else None
