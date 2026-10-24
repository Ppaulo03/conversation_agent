"""Normalization shared by every ContactMemory adapter, so they cannot disagree on what a valid
fact is."""

from __future__ import annotations

import unicodedata


def clean(text: str, max_chars: int) -> str | None:
    """One line of text, spaces collapsed; None when blank, too long or holding control
    characters other than whitespace (the model's output is not trusted to be a sentence)."""
    if any(unicodedata.category(c) in ("Cc", "Cf") and not c.isspace() for c in text):
        return None
    collapsed = " ".join(text.split())
    if not collapsed or len(collapsed) > max_chars:
        return None
    return collapsed


def same_fact(a: str, b: str) -> bool:
    return a.casefold() == b.casefold()
