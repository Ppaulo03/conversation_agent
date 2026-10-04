"""Deterministic pt-BR temporal normalisation (DESIGN §21-22).

The LLM may *spot* an expression ("amanhã às 15h"), but the final value is always computed here
from the turn's immutable reference date and the agent's timezone: nothing in this module calls
a model, reads a clock, or guesses. When an expression is ambiguous the answer is `None` (the
flow then asks), never a guess.

Conventions, all deliberate and tested:
  - a weekday name means its next occurrence AFTER today ("terça" said on a Tuesday = next week);
    "hoje" is the only way to say today;
  - a bare day ("dia 15") is this month's if it has not passed, else next month's;
  - "10h", "10:30", "às 15" are 24-hour readings; hours 1-6 without a period marker
    ("às 3", "3h") are ambiguous (03:00 or 15:00?) -> None;
  - "3 da tarde" / "8 da noite" use the period marker; "meio-dia" = 12:00.
"""

from __future__ import annotations

import re
import unicodedata
from calendar import monthrange
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

_WEEKDAYS = {
    "segunda": 0, "terca": 1, "quarta": 2, "quinta": 3, "sexta": 4, "sabado": 5, "domingo": 6,
}  # fmt: skip
_MONTHS = {
    "janeiro": 1, "jan": 1, "fevereiro": 2, "fev": 2, "marco": 3, "mar": 3, "abril": 4, "abr": 4,
    "maio": 5, "mai": 5, "junho": 6, "jun": 6, "julho": 7, "jul": 7, "agosto": 8, "ago": 8,
    "setembro": 9, "set": 9, "outubro": 10, "out": 10, "novembro": 11, "nov": 11,
    "dezembro": 12, "dez": 12,
}  # fmt: skip
_NUMBER_WORDS = {
    "um": 1, "uma": 1, "dois": 2, "duas": 2, "tres": 3, "quatro": 4, "cinco": 5, "seis": 6,
    "sete": 7, "oito": 8, "nove": 9, "dez": 10, "onze": 11, "doze": 12,
}  # fmt: skip
WEEKDAY_ABBREV = ("seg", "ter", "qua", "qui", "sex", "sáb", "dom")


def fold(text: str) -> str:
    """Lower-case and strip accents; keep digits, ':', '/', '-' and spaces."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    plain = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9:/\-\s]", " ", plain)).strip()


def reference_date(reference_time: datetime, timezone: str) -> date:
    """'Today' is the turn's reference time in the agent's timezone (INV-018)."""
    return reference_time.astimezone(ZoneInfo(timezone)).date()


# --- dates ---


def _next_weekday(today: date, weekday: int) -> date:
    delta = (weekday - today.weekday()) % 7 or 7  # strictly after today
    return today + timedelta(days=delta)


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_date(text: str, today: date) -> date | None:
    t = fold(text)
    if re.search(r"\bdepois de amanha\b", t):
        return today + timedelta(days=2)
    if re.search(r"\bamanha\b", t):
        return today + timedelta(days=1)
    if re.search(r"\bhoje\b", t):
        return today

    iso = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", t)
    if iso:
        return _safe_date(int(iso[1]), int(iso[2]), int(iso[3]))

    numeric = re.search(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b", t)
    if numeric:
        day, month = int(numeric[1]), int(numeric[2])
        if numeric[3]:
            year = int(numeric[3])
            return _safe_date(year + 2000 if year < 100 else year, month, day)
        return _upcoming(today, month, day)

    named = re.search(r"\b(\d{1,2}) (?:de )?([a-z]{3,9})\b", t)
    if named and named[2] in _MONTHS:
        return _upcoming(today, _MONTHS[named[2]], int(named[1]))

    for name, weekday in _WEEKDAYS.items():
        if re.search(rf"\b{name}\b", t):
            return _next_weekday(today, weekday)

    day_only = re.search(r"\bdia (\d{1,2})\b", t)
    if day_only:
        day = int(day_only[1])
        this_month = _safe_date(today.year, today.month, day)
        if this_month is not None and this_month >= today:
            return this_month
        year, month = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
        return _safe_date(year, month, day) if day <= monthrange(year, month)[1] else None
    return None


def _upcoming(today: date, month: int, day: int) -> date | None:
    this_year = _safe_date(today.year, month, day)
    if this_year is not None and this_year >= today:
        return this_year
    return _safe_date(today.year + 1, month, day)


# --- times ---


def _period(t: str) -> str | None:
    if re.search(r"\bda (manha|madrugada)\b", t):
        return "am"
    if re.search(r"\bda noite\b", t):
        return "night"
    if re.search(r"\bda tarde\b", t):
        return "pm"
    return None


def parse_time(text: str) -> time | None:
    """None when absent OR ambiguous."""
    t = fold(text)
    if re.search(r"\bmeio[ -]?dia\b", t):
        return time(12, 0)
    if re.search(r"\bmeia[ -]?noite\b", t):
        return time(0, 0)

    hour_text: str | None = None
    minute_text: str | None = None
    for pattern in (
        r"\b(\d{1,2}):(\d{2})\b",  # 15:30
        r"\b(\d{1,2}) ?h(?:oras?)? ?(\d{2})?\b",  # 15h, 15h30, 15 horas
        r"\b(?:as|a) (\d{1,2})(?: e (meia))?\b",  # às 15, às 10 e meia
        r"\b(\d{1,2})(?: e (meia))? (?=da (?:manha|tarde|noite|madrugada))",  # 3 da tarde
    ):
        found = re.search(pattern, t)
        if found:
            hour_text, minute_text = found[1], found[2]
            break
    if hour_text is None:
        words = "|".join(_NUMBER_WORDS)
        spelled = re.search(
            rf"\b(?:as |a )?({words})(?: e (meia))? (?=da (?:manha|tarde|noite|madrugada))", t
        )
        if spelled is None:
            return None
        hour_text, minute_text = str(_NUMBER_WORDS[spelled[1]]), spelled[2]

    hour = int(hour_text)
    minute = 30 if minute_text == "meia" else int(minute_text) if minute_text else 0
    if not (0 <= hour <= 24 and 0 <= minute <= 59):
        return None
    period = _period(t)
    if period in ("pm", "night") and hour < 12:
        hour += 12
    elif (period == "am" or period == "night") and hour == 12:
        hour = 0  # "12 da noite" is midnight, "12 da manhã" is not noon
    elif period is None and 1 <= hour <= 6:
        return None  # "às 3" / "3h": 03:00 or 15:00? Ambiguous -> the flow asks.
    return time(0, minute) if hour == 24 else time(hour, minute)


# --- durations ---


def parse_duration(text: str) -> int | None:
    """Minutes, or None. Only call this for a *duration* slot ('2h' is a time elsewhere)."""
    t = fold(text)
    # "às 15h" / "as 10:30" are clock times, never durations
    t = re.sub(
        r"\b(?:as|a) \d{1,2}(?::\d{2}| ?h(?: ?\d{2})?)?\b(?! ?(?:horas?|minutos?|min))", " ", t
    )
    if re.search(r"\bmeia hora\b", t):
        return 30
    words = "|".join(_NUMBER_WORDS)
    combos = (
        (rf"\b({words}|\d{{1,2}}) horas? e meia\b", lambda n: n * 60 + 30),
        (rf"\b({words}|\d{{1,2}}) horas? e (\d{{1,2}})(?: min\w*)?\b", None),
        (r"\b(\d{1,2}) ?h ?(\d{1,2})\b", None),
    )
    m = re.search(combos[0][0], t)
    if m:
        return _number(m[1]) * 60 + 30
    m = re.search(combos[1][0], t) or re.search(combos[2][0], t)
    if m:
        return _number(m[1]) * 60 + int(m[2])
    m = re.search(rf"\b({words}|\d{{1,3}}) ?(?:horas?|h)\b", t)
    if m:
        return _number(m[1]) * 60
    m = re.search(r"\b(\d{1,3}) ?(?:minutos?|min)\b", t)
    if m:
        return int(m[1])
    return None


def _number(token: str) -> int:
    return _NUMBER_WORDS[token] if token in _NUMBER_WORDS else int(token)


# --- presentation (deterministic, pt-BR) ---


def format_local(moment: datetime, timezone: str) -> tuple[str, str]:
    """('ter 06/10', '10:00') in the agent's timezone."""
    local = moment.astimezone(ZoneInfo(timezone))
    return (
        f"{WEEKDAY_ABBREV[local.weekday()]} {local.day:02d}/{local.month:02d}",
        f"{local.hour:02d}:{local.minute:02d}",
    )
