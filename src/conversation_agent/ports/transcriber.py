from __future__ import annotations

from typing import Protocol

from conversation_agent.core.models.media import MediaReference, Transcript, TranscriptionContext


class Transcriber(Protocol):
    async def transcribe(self, media: MediaReference, context: TranscriptionContext) -> Transcript:
        """Turns ONE audio reference into text. The adapter fetches the bytes itself (claim-check)
        and enforces its own size/type limits; raising means "could not transcribe"."""
        ...
