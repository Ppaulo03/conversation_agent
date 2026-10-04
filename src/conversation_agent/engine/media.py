"""Inbound media -> text the agent can read (DESIGN 27), as ONE journaled step.

Transcribing audio is external, non-deterministic I/O: it happens inside a journal step, so a
replayed turn reuses the recorded text instead of paying for (and possibly changing) it again.
Only references travel here; the Transcriber fetches bytes itself. Media the runtime cannot read
(images, documents, video) is NAMED, never described: the agent must not pretend to have seen it.
"""

from __future__ import annotations

from conversation_agent.core.definitions.agent import AgentDefinition
from conversation_agent.core.models.media import MediaReference, TranscriptionContext
from conversation_agent.ports.transcriber import Transcriber


class MediaNormalizer:
    def __init__(self, agent: AgentDefinition, transcriber: Transcriber | None = None) -> None:
        self._agent = agent
        self._transcriber = transcriber

    async def lines(
        self, media: tuple[MediaReference, ...], context: TranscriptionContext
    ) -> list[str]:
        texts = self._agent.media
        out: list[str] = []
        for item in media:
            if item.status == "failed":
                out.append(texts.unavailable)
            elif item.status == "rejected":
                out.append(texts.too_large if item.reason == "too_large" else texts.unsupported)
            elif item.size_bytes is not None and item.size_bytes > self._agent.max_media_bytes:
                out.append(texts.too_large)
            elif item.kind == "audio":
                out.append(await self._audio(item, context))
            elif item.kind == "image":
                out.append(texts.image)
            elif item.kind == "video":
                out.append(texts.video)
            else:
                out.append(texts.document.format(name=item.filename or item.media_id))
        return out

    async def _audio(self, item: MediaReference, context: TranscriptionContext) -> str:
        texts = self._agent.media
        if self._transcriber is None:
            return texts.audio_failed
        try:
            transcript = await self._transcriber.transcribe(item, context)
        except Exception:  # a failed transcription must never fail the turn
            return texts.audio_failed
        spoken = transcript.text.strip()
        return texts.audio.format(text=spoken) if spoken else texts.audio_failed
