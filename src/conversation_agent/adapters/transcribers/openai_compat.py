"""Speech-to-text over the OpenAI-compatible `POST /audio/transcriptions` protocol.

One adapter for every server that speaks it: OpenAI, Groq, Azure, and the local ones (a
faster-whisper server, whisper.cpp's server, LocalAI, vLLM). Where the audio goes is the OPERATOR's
choice (`base_url`); the framework only ever sends it when the operator built this adapter AND the
agent says `transcription: on`.

The adapter fetches the bytes itself (the conversation only carries a reference), enforces its own
size limit, and sends one request. Anything that goes wrong raises `TranscriptionError` carrying a
short code: never the provider's body, never the audio, never the key.
"""

from __future__ import annotations

import httpx

from conversation_agent.core.models.media import MediaReference, Transcript, TranscriptionContext
from conversation_agent.ports.media_fetcher import MediaFetcher

_EXTENSION = {
    "audio/ogg": "ogg",
    "audio/opus": "ogg",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/mp4": "m4a",
    "audio/m4a": "m4a",
    "audio/x-m4a": "m4a",
    "audio/aac": "m4a",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/webm": "webm",
    "audio/flac": "flac",
}
DEFAULT_MAX_AUDIO_BYTES = 25 * 1024 * 1024  # what the common hosted endpoints accept


class TranscriptionError(Exception):
    """The audio could not be transcribed. `code` is short and safe to log."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def filename_for(media: MediaReference) -> str:
    """Servers decide the format from the file name: derive a sensible one from the MIME type."""
    base = media.mime_type.split(";")[0].strip().lower()
    return f"audio.{_EXTENSION.get(base, 'ogg')}"  # WhatsApp voice notes are ogg/opus


class OpenAICompatTranscriber:
    def __init__(
        self,
        client: httpx.AsyncClient,
        fetcher: MediaFetcher,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        language: str | None = None,
        max_audio_bytes: int = DEFAULT_MAX_AUDIO_BYTES,
        timeout_seconds: float = 60.0,
    ) -> None:
        if not base_url:
            raise ValueError("base_url is required")
        self._client = client
        self._fetcher = fetcher
        self._url = base_url.rstrip("/") + "/audio/transcriptions"
        self._model = model
        self._api_key = api_key  # None for a local server that needs none
        self._language = language
        self._max_bytes = max_audio_bytes
        self._timeout = timeout_seconds

    @property
    def model(self) -> str:
        return self._model

    async def aclose(self) -> None:
        await self._client.aclose()

    async def transcribe(self, media: MediaReference, context: TranscriptionContext) -> Transcript:
        if media.kind != "audio":
            raise TranscriptionError("NOT_AUDIO")
        try:
            audio = await self._fetcher.fetch(context.tenant_id, media, max_bytes=self._max_bytes)
        except Exception as exc:  # not found, too large, checksum mismatch...
            raise TranscriptionError("MEDIA_UNAVAILABLE") from exc
        if len(audio) > self._max_bytes:
            raise TranscriptionError("AUDIO_TOO_LARGE")
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        data = {"model": self._model, "response_format": "verbose_json", "temperature": "0"}
        language = context.language_hint or self._language
        if language:
            data["language"] = language
        try:
            response = await self._client.post(
                self._url,
                headers=headers,
                data=data,
                files={"file": (filename_for(media), audio, media.mime_type.split(";")[0])},
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise TranscriptionError("TIMEOUT") from exc
        except httpx.HTTPError as exc:
            raise TranscriptionError("TRANSPORT_ERROR") from exc
        if response.status_code == 429:
            raise TranscriptionError("RATE_LIMITED")
        if response.status_code == 413:
            raise TranscriptionError("AUDIO_TOO_LARGE")
        if response.status_code != 200:
            raise TranscriptionError(f"HTTP_{response.status_code}")
        return self._parse(response)

    @staticmethod
    def _parse(response: httpx.Response) -> Transcript:
        try:
            body = response.json()
        except ValueError as exc:
            raise TranscriptionError("BAD_RESPONSE") from exc
        text = body.get("text") if isinstance(body, dict) else None
        if not isinstance(text, str):
            raise TranscriptionError("BAD_RESPONSE")
        language = body.get("language")
        duration = body.get("duration")
        return Transcript(
            text=text,
            language=language if isinstance(language, str) else None,
            duration_seconds=float(duration)
            if isinstance(duration, int | float)
            and not isinstance(duration, bool)
            and duration >= 0
            else None,
        )
