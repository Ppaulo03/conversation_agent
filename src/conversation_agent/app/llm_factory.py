"""Composition-root helper: picks the concrete LLM adapter from environment configuration.

  LLM_PROVIDER   groq | openai | openai_compat | anthropic   (inferred from legacy keys if unset)
  LLM_API_KEY    the key (falls back to GROQ_API_KEY / OPENAI_API_KEY / ANTHROPIC_API_KEY)
  LLM_MODEL      model id (provider default for groq / anthropic; required otherwise)
  LLM_BASE_URL   only for openai_compat

Secrets are read from the environment only and never appear in error messages.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import httpx

from conversation_agent.adapters.llm.openai_compat import (
    GROQ_BASE_URL,
    GROQ_DEFAULT_MODEL,
    OpenAICompatLLM,
)
from conversation_agent.core.errors import ConversationAgentError
from conversation_agent.ports.llm import LLMProvider

OPENAI_BASE_URL = "https://api.openai.com/v1"
ANTHROPIC_DEFAULT_MODEL = "claude-sonnet-5-5"

PROVIDERS = ("groq", "openai", "openai_compat", "anthropic")
_LEGACY_KEY_VARS = {
    "groq": "GROQ_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}
_DEFAULT_MODELS = {"groq": GROQ_DEFAULT_MODEL, "anthropic": ANTHROPIC_DEFAULT_MODEL}


class LLMConfigError(ConversationAgentError):
    """Missing or inconsistent LLM configuration (message never contains secrets)."""


@dataclass(frozen=True)
class LLMConfig:
    provider: str
    api_key: str
    model: str
    base_url: str | None = None

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str],
        *,
        provider: str | None = None,
        model: str | None = None,
    ) -> LLMConfig:
        chosen = provider or env.get("LLM_PROVIDER") or cls._infer_provider(env)
        if chosen not in PROVIDERS:
            raise LLMConfigError(f"unknown LLM provider {chosen!r}; expected one of {PROVIDERS}")
        key_var = _LEGACY_KEY_VARS.get(chosen, "LLM_API_KEY")
        api_key = env.get("LLM_API_KEY") or env.get(key_var)
        if not api_key:
            raise LLMConfigError(f"set LLM_API_KEY (or {key_var}) for provider {chosen!r}")
        resolved_model = model or env.get("LLM_MODEL") or _DEFAULT_MODELS.get(chosen)
        if not resolved_model:
            raise LLMConfigError(f"set LLM_MODEL for provider {chosen!r}")
        base_url = env.get("LLM_BASE_URL")
        if chosen == "openai_compat" and not base_url:
            raise LLMConfigError("set LLM_BASE_URL for provider 'openai_compat'")
        return cls(chosen, api_key, resolved_model, base_url)

    @staticmethod
    def _infer_provider(env: Mapping[str, str]) -> str:
        for name, var in _LEGACY_KEY_VARS.items():
            if env.get(var):
                return name
        raise LLMConfigError("set LLM_PROVIDER and LLM_API_KEY (no provider key found)")


def build_llm(config: LLMConfig) -> LLMProvider:
    if config.provider == "anthropic":
        from conversation_agent.adapters.llm.anthropic import AnthropicLLM  # extra `anthropic`

        return AnthropicLLM.from_api_key(config.api_key, config.model)
    base_url = {"groq": GROQ_BASE_URL, "openai": OPENAI_BASE_URL}.get(config.provider)
    base_url = base_url or config.base_url
    assert base_url is not None  # guaranteed by LLMConfig.from_env
    return OpenAICompatLLM(
        httpx.AsyncClient(), base_url=base_url, api_key=config.api_key, model=config.model
    )


async def close_llm(llm: LLMProvider) -> None:
    """Releases HTTP clients held by real adapters (no-op for fakes)."""
    close = getattr(llm, "aclose", None)  # real adapters hold an HTTP client; fakes do not
    if close is not None:
        await close()
