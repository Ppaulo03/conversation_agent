"""MeteredTranscriber: every transcription is written to the books, by the second of audio.

Same contract as `MeteredLLMProvider`: it reads who and why from the observability context, times
the call, and records one `LLMCallRecord` (purpose `transcription`, the audio seconds, never the
audio or the text). A failure to WRITE the record is logged, never propagated: losing a usage
row must not turn a customer's message into an error. A journal replay reuses the stored text
and never reaches the transcriber, so it is not billed twice.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime

from conversation_agent.core.models.llm_usage import UNATTRIBUTED_TENANT, LLMCallRecord
from conversation_agent.core.models.media import MediaReference, Transcript, TranscriptionContext
from conversation_agent.core.observability import current
from conversation_agent.ports.llm_usage import LLMUsageStore
from conversation_agent.ports.transcriber import Transcriber

log = logging.getLogger(__name__)


class MeteredTranscriber:
    def __init__(
        self,
        inner: Transcriber,
        store: LLMUsageStore | None,
        *,
        provider_name: str = "unknown",
        model_name: str = "unknown",
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._inner = inner
        self._store = store
        self._provider = provider_name
        self._model = model_name
        self._monotonic = monotonic
        self._now = now

    async def transcribe(self, media: MediaReference, context: TranscriptionContext) -> Transcript:
        started_at, started = self._now(), self._monotonic()
        try:
            transcript = await self._inner.transcribe(media, context)
        except Exception as exc:  # a call that happened (and may have been billed)
            await self._record(media, started_at, started, None, type(exc).__name__)
            raise
        await self._record(media, started_at, started, transcript, None)
        return transcript

    async def _record(
        self,
        media: MediaReference,
        started_at: datetime,
        started: float,
        transcript: Transcript | None,
        error_code: str | None,
    ) -> None:
        if self._store is None:
            return
        ctx = current()
        seconds = (transcript.duration_seconds if transcript else None) or media.seconds or 0
        record = LLMCallRecord(
            tenant_id=ctx.get("tenant_id", UNATTRIBUTED_TENANT),
            started_at=started_at,
            purpose="transcription",
            agent_id=ctx.get("agent_id"),
            agent_version=ctx.get("agent_version"),
            conversation_ref=ctx.get("conversation_ref"),
            turn_id=ctx.get("turn_id"),
            trace_id=ctx.get("trace_id"),
            request_id=media.media_id,
            provider=self._provider,
            model=self._model,
            audio_seconds=float(seconds),
            latency_ms=(self._monotonic() - started) * 1000,
            outcome="ok" if error_code is None else "error",
            error_code=error_code,
        )
        try:
            await self._store.record(record)
        except Exception as exc:  # never let the books break the answer
            log.error(
                "llm_usage.record_failed",
                extra={"fields": {"alert": True, "error": type(exc).__name__}},
            )
