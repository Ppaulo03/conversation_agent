from __future__ import annotations

from collections.abc import Callable

from conversation_agent.core.models.media import MediaReference, Transcript, TranscriptionContext


class FakeTranscriber:
    """Deterministic stand-in: `answers` maps media_id -> text (or a callable)."""

    def __init__(
        self, answers: dict[str, str] | Callable[[MediaReference], str] | None = None
    ) -> None:
        self._answers = answers or {}
        self.calls: list[MediaReference] = []
        self.fail_with: Exception | None = None

    async def transcribe(self, media: MediaReference, context: TranscriptionContext) -> Transcript:
        self.calls.append(media)
        if self.fail_with is not None:
            raise self.fail_with
        if callable(self._answers):
            return Transcript(text=self._answers(media))
        return Transcript(text=self._answers.get(media.media_id, ""), language="pt")
