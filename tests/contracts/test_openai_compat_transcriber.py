"""Phase 15a: the OpenAI-compatible speech-to-text adapter (OpenAI, Groq, local servers).

No network: an httpx MockTransport plays the server. What matters is what leaves (the file, the
model, the key only when there is one) and that nothing sensitive ever comes back out in an error.
"""

from __future__ import annotations

import httpx
import pytest

from conversation_agent.adapters.transcribers.openai_compat import (
    OpenAICompatTranscriber,
    TranscriptionError,
    filename_for,
)
from conversation_agent.core.models.media import MediaReference, TranscriptionContext

SECRET = "sk-very-secret-key"
AUDIO = b"OggS\x00fake-opus-bytes"
CONTEXT = TranscriptionContext(tenant_id="tenant-7", conversation_id="c", turn_id="t")


def voice(mime: str = "audio/ogg; codecs=opus", **kw: object) -> MediaReference:
    return MediaReference(media_id="m1", kind="audio", mime_type=mime, **kw)  # type: ignore[arg-type]


class Fetcher:
    def __init__(self, audio: bytes = AUDIO, fail: Exception | None = None) -> None:
        self.audio, self.fail = audio, fail
        self.calls: list[tuple[str, int]] = []

    async def fetch(self, tenant_id: str, media: MediaReference, *, max_bytes: int) -> bytes:
        self.calls.append((tenant_id, max_bytes))
        if self.fail is not None:
            raise self.fail
        return self.audio


def server(handler: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


def make(
    handler: object, fetcher: Fetcher | None = None, **kw: object
) -> tuple[OpenAICompatTranscriber, Fetcher]:
    fetcher = fetcher or Fetcher()
    kw.setdefault("base_url", "https://stt.example/v1/")
    kw.setdefault("model", "whisper-large-v3-turbo")
    return OpenAICompatTranscriber(server(handler), fetcher, **kw), fetcher  # type: ignore[arg-type]


def ok(text: str = "queria saber se vocês abrem sábado", **extra: object) -> httpx.Response:
    return httpx.Response(
        200, json={"text": text, "language": "portuguese", "duration": 4.2, **extra}
    )


async def test_the_audio_is_sent_as_a_file_with_the_model_and_the_key_and_the_text_comes_back() -> (
    None
):
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["type"] = request.headers["content-type"]
        seen["body"] = request.read()
        return ok()

    transcriber, fetcher = make(handler, api_key=SECRET, language="pt")
    transcript = await transcriber.transcribe(voice(), CONTEXT)

    assert transcript.text == "queria saber se vocês abrem sábado"
    assert transcript.language == "portuguese" and transcript.duration_seconds == 4.2
    assert seen["url"] == "https://stt.example/v1/audio/transcriptions"
    assert seen["auth"] == f"Bearer {SECRET}"
    body = seen["body"]
    assert isinstance(body, bytes) and AUDIO in body  # the file itself
    for field in (b'name="model"', b"whisper-large-v3-turbo", b'name="language"', b"pt"):
        assert field in body
    assert b'filename="audio.ogg"' in body and b"verbose_json" in body
    assert fetcher.calls == [("tenant-7", 25 * 1024 * 1024)]  # the tenant's own media, bounded


async def test_a_local_server_needs_no_key_and_none_is_sent() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return ok()

    transcriber, _ = make(handler, base_url="http://localhost:8000/v1")
    await transcriber.transcribe(voice(), CONTEXT)
    assert seen["auth"] is None


async def test_a_language_hint_from_the_runtime_wins_over_the_configured_one() -> None:
    seen: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.read())
        return ok()

    transcriber, _ = make(handler, language="pt")
    await transcriber.transcribe(voice(), CONTEXT.model_copy(update={"language_hint": "en"}))
    assert b'name="language"\r\n\r\nen' in seen[0]


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (httpx.Response(429, text="slow down, key sk-very-secret-key"), "RATE_LIMITED"),
        (httpx.Response(413, text="too big"), "AUDIO_TOO_LARGE"),
        (httpx.Response(500, text=f"boom {SECRET}"), "HTTP_500"),
        (httpx.Response(401, json={"error": {"message": SECRET}}), "HTTP_401"),
        (httpx.Response(200, text="<html>not json</html>"), "BAD_RESPONSE"),
        (httpx.Response(200, json={"nothing": "here"}), "BAD_RESPONSE"),
        (httpx.Response(200, json=["not", "an", "object"]), "BAD_RESPONSE"),
    ],
)
async def test_a_server_that_fails_raises_a_short_code_and_never_its_body_or_the_key(
    response: httpx.Response, code: str
) -> None:
    transcriber, _ = make(lambda request: response, api_key=SECRET)
    with pytest.raises(TranscriptionError) as caught:
        await transcriber.transcribe(voice(), CONTEXT)
    assert caught.value.code == code
    assert SECRET not in str(caught.value) and "slow down" not in str(caught.value)


async def test_a_timeout_and_a_dead_server_are_distinct_short_codes() -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("took too long", request=request)

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    for handler, code in ((slow, "TIMEOUT"), (down, "TRANSPORT_ERROR")):
        transcriber, _ = make(handler)
        with pytest.raises(TranscriptionError) as caught:
            await transcriber.transcribe(voice(), CONTEXT)
        assert caught.value.code == code


async def test_audio_that_cannot_be_fetched_never_reaches_the_server() -> None:
    called: list[bool] = []

    def handler(request: httpx.Request) -> httpx.Response:
        called.append(True)
        return ok()

    transcriber, _ = make(handler, Fetcher(fail=RuntimeError("checksum mismatch")))
    with pytest.raises(TranscriptionError) as caught:
        await transcriber.transcribe(voice(), CONTEXT)
    assert caught.value.code == "MEDIA_UNAVAILABLE" and called == []


async def test_the_size_limit_is_the_adapters_own_and_is_enforced_twice() -> None:
    called: list[bool] = []

    def handler(request: httpx.Request) -> httpx.Response:
        called.append(True)
        return ok()

    transcriber, fetcher = make(handler, Fetcher(audio=b"x" * 100), max_audio_bytes=50)
    with pytest.raises(TranscriptionError) as caught:  # a fetcher that ignored max_bytes
        await transcriber.transcribe(voice(), CONTEXT)
    assert caught.value.code == "AUDIO_TOO_LARGE" and called == []
    assert fetcher.calls == [("tenant-7", 50)]  # and it asked the fetcher for at most 50


async def test_only_audio_is_sent() -> None:
    transcriber, fetcher = make(lambda request: ok())
    image = MediaReference(media_id="i", kind="image", mime_type="image/jpeg")
    with pytest.raises(TranscriptionError) as caught:
        await transcriber.transcribe(image, CONTEXT)
    assert caught.value.code == "NOT_AUDIO" and fetcher.calls == []


@pytest.mark.parametrize(
    ("mime", "name"),
    [
        ("audio/ogg; codecs=opus", "audio.ogg"),
        ("audio/mpeg", "audio.mp3"),
        ("audio/mp4", "audio.m4a"),
        ("audio/AAC", "audio.m4a"),
        ("audio/wav", "audio.wav"),
        ("audio/webm", "audio.webm"),
        ("application/octet-stream", "audio.ogg"),  # WhatsApp voice notes are ogg/opus
    ],
)
def test_servers_decide_the_format_from_the_name_so_it_follows_the_mime_type(
    mime: str, name: str
) -> None:
    assert filename_for(voice(mime)) == name


async def test_a_response_without_a_duration_still_works() -> None:
    transcriber, _ = make(lambda request: httpx.Response(200, json={"text": "oi"}))  # json format
    transcript = await transcriber.transcribe(voice(), CONTEXT)
    assert transcript.text == "oi" and transcript.duration_seconds is None
