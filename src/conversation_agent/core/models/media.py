"""Claim-check references to inbound media (DESIGN 27).

The conversation never carries media BYTES: only a small reference (where it lives, what it is,
how big, its hash). Bytes are fetched on demand by the adapter that needs them (a Transcriber),
so state, journal and prompts stay small and free of binary payloads.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

MediaKind = Literal["audio", "image", "document", "video"]
MAX_MEDIA_ITEMS = 10  # per inbound event: a message with more is refused at the edge


class MediaReference(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    media_id: str = Field(min_length=1, max_length=200)
    kind: MediaKind
    mime_type: str = Field(max_length=100)
    size_bytes: int | None = Field(default=None, ge=0)
    url: str | None = Field(default=None, max_length=2000)  # a reference, never fetched here
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    filename: str | None = Field(default=None, max_length=200)


class Transcript(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    language: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)


class TranscriptionContext(BaseModel):
    """What a Transcriber may know: trusted runtime facts only (never secrets)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str
    conversation_id: str
    turn_id: str
    language_hint: str | None = None
