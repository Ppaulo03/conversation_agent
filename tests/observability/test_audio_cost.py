"""Phase 15a: speech-to-text is on the books by the SECOND of audio and priced per minute."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from conversation_agent.adapters.llm.usage_memory import InMemoryLLMUsageStore
from conversation_agent.adapters.transcribers.fake import FakeTranscriber
from conversation_agent.adapters.transcribers.metered import MeteredTranscriber
from conversation_agent.core.llm_prices import ModelPrice, PriceTable, cost_of
from conversation_agent.core.models.llm_usage import LLMCallRecord, UsageQuery
from conversation_agent.core.models.media import MediaReference, Transcript, TranscriptionContext
from conversation_agent.core.observability import bind

AT = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
CONTEXT = TranscriptionContext(tenant_id="t1", conversation_id="c", turn_id="turn-1")


def prices() -> PriceTable:
    return PriceTable(
        models={
            "groq/whisper-large-v3-turbo": (
                ModelPrice(
                    effective_from=date(2026, 1, 1),
                    input_per_mtok=0,
                    output_per_mtok=0,
                    audio_per_minute=0.0006,
                ),
            ),
            "groq/token-only-model": (
                ModelPrice(effective_from=date(2026, 1, 1), input_per_mtok=1, output_per_mtok=1),
            ),
        }
    )


def test_audio_costs_what_a_minute_costs_per_the_seconds_heard() -> None:
    cost = cost_of(
        prices(),
        provider="groq",
        model="whisper-large-v3-turbo",
        at=AT,
        input_tokens=0,
        output_tokens=0,
        audio_seconds=90,
    )
    assert cost == pytest.approx(0.0009)  # 1.5 min x 0.0006


def test_audio_with_no_audio_price_is_reported_as_unpriced_never_as_free() -> None:
    cost = cost_of(
        prices(),
        provider="groq",
        model="token-only-model",
        at=AT,
        input_tokens=0,
        output_tokens=0,
        audio_seconds=30,
    )
    assert cost is None
    # and a model nobody priced at all is also a visible gap
    assert (
        cost_of(prices(), provider="x", model="y", at=AT, input_tokens=0, output_tokens=0) is None
    )


def test_text_calls_cost_exactly_what_they_did_before_audio_existed() -> None:
    cost = cost_of(
        prices(),
        provider="groq",
        model="token-only-model",
        at=AT,
        input_tokens=1_000_000,
        output_tokens=0,
    )
    assert cost == pytest.approx(1.0)


async def test_a_transcription_is_recorded_with_its_seconds_never_the_audio_or_the_text() -> None:
    store = InMemoryLLMUsageStore()
    inner = FakeTranscriber(lambda media: "texto secreto da pessoa")
    metered = MeteredTranscriber(inner, store, provider_name="groq", model_name="whisper")
    media = MediaReference(media_id="m1", kind="audio", mime_type="audio/ogg", seconds=12)
    with bind(tenant_id="t1", agent_id="a", agent_version="1", turn_id="turn-1"):
        transcript = await metered.transcribe(media, CONTEXT)
    assert transcript.text == "texto secreto da pessoa"

    (record,) = store.records
    assert record.purpose == "transcription" and record.outcome == "ok"
    assert record.audio_seconds == 12 and record.tenant_id == "t1"  # the channel's length
    assert record.provider == "groq" and record.model == "whisper"
    assert "secreto" not in record.model_dump_json()  # numbers and ids only


async def test_the_providers_own_measure_of_the_duration_wins_over_the_channels() -> None:
    class Measuring:
        async def transcribe(
            self, media: MediaReference, context: TranscriptionContext
        ) -> Transcript:
            return Transcript(text="x", duration_seconds=7.5)

    store = InMemoryLLMUsageStore()
    media = MediaReference(media_id="m1", kind="audio", mime_type="audio/ogg", seconds=99)
    await MeteredTranscriber(Measuring(), store).transcribe(media, CONTEXT)
    assert store.records[0].audio_seconds == 7.5


async def test_a_failed_transcription_is_recorded_as_an_error_and_still_raises() -> None:
    store = InMemoryLLMUsageStore()
    inner = FakeTranscriber()
    inner.fail_with = RuntimeError("provider down")
    media = MediaReference(media_id="m1", kind="audio", mime_type="audio/ogg", seconds=5)
    with pytest.raises(RuntimeError):
        await MeteredTranscriber(inner, store).transcribe(media, CONTEXT)
    (record,) = store.records
    assert record.outcome == "error" and record.error_code == "RuntimeError"


async def test_a_ledger_that_cannot_write_never_breaks_the_message() -> None:
    class Broken:
        async def record(self, call: LLMCallRecord) -> None:
            raise OSError("disk full")

    media = MediaReference(media_id="m1", kind="audio", mime_type="audio/ogg")
    transcript = await MeteredTranscriber(FakeTranscriber({"m1": "oi"}), Broken()).transcribe(  # type: ignore[arg-type]
        media, CONTEXT
    )
    assert transcript.text == "oi"


async def test_the_report_adds_the_minutes_and_prices_them() -> None:
    store = InMemoryLLMUsageStore()
    for seconds in (30, 60, 90):
        await store.record(
            LLMCallRecord(
                tenant_id="t1",
                started_at=AT,
                purpose="transcription",
                provider="groq",
                model="whisper-large-v3-turbo",
                audio_seconds=seconds,
            )
        )
    query = UsageQuery(
        tenant_id="t1",
        since=datetime(2026, 10, 1, tzinfo=UTC),
        until=datetime(2026, 11, 1, tzinfo=UTC),
        group_by=("purpose",),
    )
    (row,) = await store.report(query, prices())
    assert row.audio_seconds == 180 and row.calls == 3
    assert row.cost_usd == pytest.approx(0.0018) and row.unpriced_calls == 0
