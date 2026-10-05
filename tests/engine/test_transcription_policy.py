"""Phase 15a: voice is only transcribed when BOTH the operator and the agent say so (INV-067).

The audio can go to a third-party API, and a voice is personal data: a transcriber being configured
is not enough, the agent must ask for it, and the default is that it does not.
"""

from __future__ import annotations

import httpx

from conversation_agent.adapters.llm.fake import FakeLLM, text_response
from conversation_agent.adapters.manifest.yaml_loader import load_manifest_file
from conversation_agent.adapters.transcribers.fake import FakeTranscriber
from conversation_agent.adapters.transcribers.openai_compat import OpenAICompatTranscriber
from conversation_agent.core.compiler import agent_document, compile_manifest
from conversation_agent.core.definitions.agent import MediaTexts
from conversation_agent.core.models.media import MediaReference
from support.builders import IDENTITY, new_clock, new_journal
from vertical_slice.definitions import build_agent
from vertical_slice.wiring import MANIFEST_PATH, build_engine

OFF = "[voice message received; this assistant does not process voice messages]"


def voice() -> MediaReference:
    return MediaReference(media_id="a1", kind="audio", mime_type="audio/ogg", size_bytes=5000)


async def heard(transcription: str, transcriber: object, **kw: object) -> tuple[str, FakeLLM]:
    llm = FakeLLM([text_response("ok")])
    agent = build_agent(transcription=transcription)
    if kw:
        agent = agent.model_copy(update=kw)
    engine, _, _ = build_engine(
        llm,
        api_base_url="http://unused",
        journal=new_journal(),
        clock=new_clock(),
        transcriber=transcriber,  # type: ignore[arg-type]
        agent=agent,
    )
    from conversation_agent.core.models.conversation import ConversationState

    await engine.process_turn(IDENTITY, ConversationState(), "", turn_id="t1", media=(voice(),))
    text = "".join(p.text for p in llm.requests[0].messages[-1].parts if hasattr(p, "text"))
    return text, llm


async def test_by_default_a_configured_transcriber_is_never_used() -> None:
    transcriber = FakeTranscriber({"a1": "isto não deveria ser ouvido"})
    text, _ = await heard("off", transcriber)
    assert text == OFF  # the audio is named, not heard...
    assert transcriber.calls == []  # ...and nothing was handed to the transcriber


async def test_an_agent_that_asks_for_it_gets_the_transcript() -> None:
    transcriber = FakeTranscriber({"a1": "quero marcar um corte"})
    text, _ = await heard("on", transcriber)
    assert text == "[voice message] quero marcar um corte" and len(transcriber.calls) == 1


async def test_asking_for_it_without_an_operator_transcriber_says_it_could_not() -> None:
    text, _ = await heard("on", None)
    assert text == "[voice message that could not be transcribed]"


async def test_the_wording_when_off_is_the_agents_to_translate() -> None:
    texts = MediaTexts(audio_disabled="[mensagem de voz recebida; não processo áudios]")
    text, _ = await heard("off", FakeTranscriber(), media=texts)
    assert text == "[mensagem de voz recebida; não processo áudios]"


async def test_the_real_adapter_is_not_even_called_for_an_agent_that_did_not_ask() -> None:
    reached: list[bool] = []

    def handler(request: httpx.Request) -> httpx.Response:
        reached.append(True)
        return httpx.Response(200, json={"text": "x"})

    class Fetcher:
        async def fetch(self, tenant_id: str, media: MediaReference, *, max_bytes: int) -> bytes:
            raise AssertionError("the audio must not even be fetched")

    adapter = OpenAICompatTranscriber(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        Fetcher(),
        base_url="https://stt.example/v1",
        model="m",
    )
    text, _ = await heard("off", adapter)
    assert text == OFF and reached == []  # no request left the process


def test_agents_that_do_not_use_it_keep_the_digest_they_always_had() -> None:
    raw = load_manifest_file(MANIFEST_PATH)
    off = compile_manifest(raw)
    document = agent_document(off.agent)
    assert "transcription" not in document and "audio_disabled" not in document["media"]

    raw["transcription"] = "on"
    on = compile_manifest(raw)
    assert agent_document(on.agent)["transcription"] == "on"
    assert on.digest != off.digest  # a real change of behaviour is a new version

    raw["transcription"] = "off"
    raw["media"] = {"audio_disabled": "[sem áudio]"}
    assert agent_document(compile_manifest(raw).agent)["media"]["audio_disabled"] == "[sem áudio]"
