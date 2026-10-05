"""Phase 15a: speech-to-text is configured from the environment, and absent means off."""

from __future__ import annotations

import httpx
import pytest

from conversation_agent.adapters.llm.usage_memory import InMemoryLLMUsageStore
from conversation_agent.adapters.transcribers.metered import MeteredTranscriber
from conversation_agent.adapters.transcribers.openai_compat import OpenAICompatTranscriber
from conversation_agent.app.stt_factory import STTConfig, STTConfigError, build_transcriber
from conversation_agent.core.models.media import MediaReference


class Fetcher:
    async def fetch(self, tenant_id: str, media: MediaReference, *, max_bytes: int) -> bytes:
        return b"x"


def test_nothing_configured_means_no_transcriber() -> None:
    assert STTConfig.from_env({}) is None
    assert STTConfig.from_env({"GROQ_API_KEY": "k"}) is None  # an LLM key alone turns nothing on


def test_groq_is_a_short_configuration_and_reuses_the_llm_key() -> None:
    config = STTConfig.from_env(
        {"STT_PROVIDER": "groq", "GROQ_API_KEY": "gsk-1", "STT_LANGUAGE": "pt"}
    )
    assert config is not None
    assert config.base_url == "https://api.groq.com/openai/v1"
    assert config.model == "whisper-large-v3-turbo" and config.api_key == "gsk-1"
    assert config.language == "pt"


def test_openai_defaults() -> None:
    config = STTConfig.from_env({"STT_PROVIDER": "openai", "STT_API_KEY": "sk-1"})
    assert config is not None and config.model == "whisper-1"
    assert config.base_url == "https://api.openai.com/v1"


def test_a_local_server_needs_a_url_and_a_model_but_no_key() -> None:
    config = STTConfig.from_env(
        {
            "STT_PROVIDER": "openai_compat",
            "STT_BASE_URL": "http://localhost:8000/v1",
            "STT_MODEL": "Systran/faster-whisper-small",
        }
    )
    assert config is not None and config.api_key is None
    assert config.base_url == "http://localhost:8000/v1"


@pytest.mark.parametrize(
    "env",
    [
        {"STT_PROVIDER": "whisperx"},  # unknown
        {"STT_PROVIDER": "groq"},  # a hosted API without a key
        {"STT_PROVIDER": "openai_compat", "STT_MODEL": "m"},  # no URL
        {"STT_PROVIDER": "openai_compat", "STT_BASE_URL": "http://x/v1"},  # no model
        {"STT_PROVIDER": "openai", "STT_API_KEY": "k", "STT_MAX_AUDIO_BYTES": "lots"},
        {"STT_PROVIDER": "openai", "STT_API_KEY": "k", "STT_MAX_AUDIO_BYTES": "0"},
    ],
)
def test_a_wrong_configuration_is_refused_without_echoing_a_secret(env: dict[str, str]) -> None:
    with pytest.raises(STTConfigError) as caught:
        STTConfig.from_env({**env, "SECRET": "sk-never-shown"})
    assert "sk-never-shown" not in str(caught.value)


async def test_the_transcriber_is_metered_when_there_are_books_to_write_to() -> None:
    config = STTConfig.from_env({"STT_PROVIDER": "groq", "STT_API_KEY": "k"})
    assert config is not None
    client = httpx.AsyncClient()
    plain = build_transcriber(config, Fetcher(), client=client)
    metered = build_transcriber(config, Fetcher(), usage=InMemoryLLMUsageStore(), client=client)
    assert isinstance(plain, OpenAICompatTranscriber)
    assert isinstance(metered, MeteredTranscriber)
    await client.aclose()
