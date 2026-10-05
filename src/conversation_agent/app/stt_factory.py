"""Composition-root helper: builds the speech-to-text adapter from environment configuration.

  STT_PROVIDER    groq | openai | openai_compat      (unset: no transcriber, voice messages are
                                                       named but never sent anywhere)
  STT_API_KEY     the key (falls back to GROQ_API_KEY / OPENAI_API_KEY; optional for a local server)
  STT_MODEL       model id (defaults: groq whisper-large-v3-turbo, openai whisper-1; required for
                  openai_compat)
  STT_BASE_URL    required for openai_compat (e.g. http://localhost:8000/v1 for a local server)
  STT_LANGUAGE    ISO-639-1 hint such as `pt` (optional; improves short audio)
  STT_MAX_AUDIO_BYTES  bytes (default 25 MiB)

The transcriber only runs for agents that say `transcription: on`. Secrets are read from the
environment only and never appear in error messages.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import httpx

from conversation_agent.adapters.transcribers.metered import MeteredTranscriber
from conversation_agent.adapters.transcribers.openai_compat import (
    DEFAULT_MAX_AUDIO_BYTES,
    OpenAICompatTranscriber,
)
from conversation_agent.core.errors import ConversationAgentError
from conversation_agent.ports.llm_usage import LLMUsageStore
from conversation_agent.ports.media_fetcher import MediaFetcher
from conversation_agent.ports.transcriber import Transcriber

STT_PROVIDERS = ("groq", "openai", "openai_compat")
_BASE_URLS = {"groq": "https://api.groq.com/openai/v1", "openai": "https://api.openai.com/v1"}
_DEFAULT_MODELS = {"groq": "whisper-large-v3-turbo", "openai": "whisper-1"}
_LEGACY_KEYS = {"groq": "GROQ_API_KEY", "openai": "OPENAI_API_KEY"}


class STTConfigError(ConversationAgentError):
    """Missing or inconsistent speech-to-text configuration (never contains secrets)."""


@dataclass(frozen=True)
class STTConfig:
    provider: str
    model: str
    base_url: str
    api_key: str | None = None
    language: str | None = None
    max_audio_bytes: int = DEFAULT_MAX_AUDIO_BYTES

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> STTConfig | None:
        """None when `STT_PROVIDER` is not set: transcription is simply not configured."""
        provider = env.get("STT_PROVIDER")
        if not provider:
            return None
        if provider not in STT_PROVIDERS:
            raise STTConfigError(
                f"unknown STT provider {provider!r}; expected one of {STT_PROVIDERS}"
            )
        model = env.get("STT_MODEL") or _DEFAULT_MODELS.get(provider)
        if not model:
            raise STTConfigError("STT_MODEL is required for openai_compat")
        base_url = env.get("STT_BASE_URL") or _BASE_URLS.get(provider)
        if not base_url:
            raise STTConfigError("STT_BASE_URL is required for openai_compat")
        key = env.get("STT_API_KEY") or env.get(_LEGACY_KEYS.get(provider, ""), "") or None
        if provider in _LEGACY_KEYS and not key:
            raise STTConfigError(f"set STT_API_KEY (or {_LEGACY_KEYS[provider]}) for {provider}")
        raw_max = env.get("STT_MAX_AUDIO_BYTES")
        try:
            max_bytes = int(raw_max) if raw_max else DEFAULT_MAX_AUDIO_BYTES
        except ValueError as exc:
            raise STTConfigError("STT_MAX_AUDIO_BYTES must be an integer") from exc
        if max_bytes <= 0:
            raise STTConfigError("STT_MAX_AUDIO_BYTES must be positive")
        return cls(
            provider=provider,
            model=model,
            base_url=base_url,
            api_key=key,
            language=env.get("STT_LANGUAGE") or None,
            max_audio_bytes=max_bytes,
        )


def build_transcriber(
    config: STTConfig,
    fetcher: MediaFetcher,
    *,
    usage: LLMUsageStore | None = None,
    client: httpx.AsyncClient | None = None,
) -> Transcriber:
    """The adapter, wrapped so every call is recorded by the second of audio when a usage store is
    given."""
    inner = OpenAICompatTranscriber(
        client or httpx.AsyncClient(),
        fetcher,
        base_url=config.base_url,
        model=config.model,
        api_key=config.api_key,
        language=config.language,
        max_audio_bytes=config.max_audio_bytes,
    )
    if usage is None:
        return inner
    return MeteredTranscriber(inner, usage, provider_name=config.provider, model_name=config.model)
