"""Inbound media -> text for the agent (claim-check; transcription is one journaled step)."""

from __future__ import annotations

from conversation_agent.adapters.llm.fake import FakeLLM, text_response
from conversation_agent.adapters.transcribers.fake import FakeTranscriber
from conversation_agent.core.models.conversation import ConversationState
from conversation_agent.core.models.journal import JournalStepType
from conversation_agent.core.models.llm import LLMRequest
from conversation_agent.core.models.media import MediaReference
from support.builders import IDENTITY, new_clock, new_journal
from vertical_slice.definitions import build_agent
from vertical_slice.wiring import build_engine


def audio(media_id: str = "a1", size: int | None = 5000) -> MediaReference:
    return MediaReference(media_id=media_id, kind="audio", mime_type="audio/ogg", size_bytes=size)


def engine_with(llm: FakeLLM, transcriber: FakeTranscriber | None, journal=None):  # type: ignore[no-untyped-def]
    engine, _, _ = build_engine(
        llm,
        api_base_url="http://unused",
        journal=journal or new_journal(),
        clock=new_clock(),
        transcriber=transcriber,
        agent=build_agent(transcription="on"),  # voice is only transcribed when the agent says so
    )
    return engine


def seen_text(llm: FakeLLM) -> str:
    request: LLMRequest = llm.requests[0]
    return "".join(part.text for part in request.messages[-1].parts if hasattr(part, "text"))


async def test_a_voice_message_reaches_the_agent_as_its_transcript() -> None:
    llm = FakeLLM([text_response("ok")])
    transcriber = FakeTranscriber({"a1": "quero marcar um corte amanhã"})
    outcome = await engine_with(llm, transcriber).process_turn(
        IDENTITY, ConversationState(), "", "t1", media=(audio(),)
    )
    assert "[voice message] quero marcar um corte amanhã" in seen_text(llm)
    assert outcome.state.history[0].text.startswith("[voice message]")  # and it is the history


async def test_text_and_voice_in_one_burst_are_both_kept() -> None:
    llm = FakeLLM([text_response("ok")])
    engine = engine_with(llm, FakeTranscriber({"a1": "às dez"}))
    await engine.process_turn(
        IDENTITY, ConversationState(), "Pode ser amanhã", "t1", media=(audio(),)
    )
    assert seen_text(llm) == "Pode ser amanhã\n[voice message] às dez"


async def test_a_replayed_turn_never_transcribes_again() -> None:
    journal = new_journal()
    transcriber = FakeTranscriber({"a1": "primeira versão"})
    first_llm = FakeLLM([text_response("ok")])
    first = await engine_with(first_llm, transcriber, journal).process_turn(
        IDENTITY, ConversationState(), "", "t1", media=(audio(),)
    )
    transcriber._answers = {"a1": "OUTRA transcrição"}  # a retry would hear something else
    replay_llm = FakeLLM([])  # nothing may be re-asked either
    again = await engine_with(replay_llm, transcriber, journal).process_turn(
        IDENTITY, ConversationState(), "", "t1", media=(audio(),)
    )
    assert len(transcriber.calls) == 1 and replay_llm.calls == 0
    assert again.reply == first.reply and again.state.history == first.state.history
    steps = [e.step_type for e in await journal.entries("t1")]  # type: ignore[attr-defined]
    assert steps.count(JournalStepType.MEDIA_NORMALIZED) == 1


async def test_a_failed_or_empty_transcription_never_fails_the_turn() -> None:
    for transcriber in (FakeTranscriber({"a1": "   "}), FakeTranscriber(), None):
        llm = FakeLLM([text_response("ok")])
        engine = engine_with(llm, transcriber)
        await engine.process_turn(IDENTITY, ConversationState(), "", "t1", media=(audio(),))
        assert seen_text(llm) == "[voice message that could not be transcribed]"
    broken = FakeTranscriber()
    broken.fail_with = TimeoutError("provider down")
    llm = FakeLLM([text_response("ok")])
    await engine_with(llm, broken).process_turn(
        IDENTITY, ConversationState(), "", "t1", media=(audio(),)
    )
    assert "could not be transcribed" in seen_text(llm)


async def test_oversized_media_is_never_fetched_or_transcribed() -> None:
    transcriber = FakeTranscriber({"big": "x"})
    llm = FakeLLM([text_response("ok")])
    huge = audio("big", size=500 * 1024 * 1024)
    await engine_with(llm, transcriber).process_turn(
        IDENTITY, ConversationState(), "", "t1", media=(huge,)
    )
    assert transcriber.calls == [] and seen_text(llm) == "[media too large to process]"


async def test_media_the_runtime_cannot_read_is_named_not_described() -> None:
    llm = FakeLLM([text_response("ok")])
    items = (
        MediaReference(media_id="i1", kind="image", mime_type="image/png"),
        MediaReference(media_id="v1", kind="video", mime_type="video/mp4"),
        MediaReference(
            media_id="d1", kind="document", mime_type="application/pdf", filename="laudo.pdf"
        ),
    )
    await engine_with(llm, None).process_turn(IDENTITY, ConversationState(), "", "t1", media=items)
    assert seen_text(llm).splitlines() == [
        "[image received]",
        "[video received]",
        "[document received: laudo.pdf]",
    ]


async def test_a_turn_without_media_has_no_media_step() -> None:
    journal = new_journal()
    await engine_with(FakeLLM([text_response("ok")]), None, journal).process_turn(
        IDENTITY, ConversationState(), "oi", "t1"
    )
    steps = [e.step_type for e in await journal.entries("t1")]  # type: ignore[attr-defined]
    assert JournalStepType.MEDIA_NORMALIZED not in steps


def test_media_references_cannot_smuggle_bytes_or_unbounded_fields() -> None:
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        MediaReference.model_validate(
            {"media_id": "x", "kind": "audio", "mime_type": "a/b", "bytes": "AAAA"}
        )
    with pytest.raises(ValidationError):
        MediaReference(media_id="x", kind="audio", mime_type="a/b", sha256="not-a-hash")
    with pytest.raises(ValidationError):
        MediaReference(media_id="x" * 201, kind="audio", mime_type="a/b")
