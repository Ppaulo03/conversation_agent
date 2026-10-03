"""LLM configuration resolution (composition root)."""

from __future__ import annotations

import pytest

from conversation_agent.adapters.llm.anthropic import AnthropicLLM
from conversation_agent.adapters.llm.openai_compat import GROQ_DEFAULT_MODEL, OpenAICompatLLM
from conversation_agent.app.llm_factory import LLMConfig, LLMConfigError, build_llm


def test_generic_variables_select_provider_key_and_model() -> None:
    cfg = LLMConfig.from_env(
        {"LLM_PROVIDER": "groq", "LLM_API_KEY": "k", "LLM_MODEL": "some-model"}
    )
    assert (cfg.provider, cfg.api_key, cfg.model) == ("groq", "k", "some-model")


def test_legacy_groq_key_alone_still_works_and_is_inferred() -> None:
    cfg = LLMConfig.from_env({"GROQ_API_KEY": "legacy"})
    assert (cfg.provider, cfg.api_key, cfg.model) == ("groq", "legacy", GROQ_DEFAULT_MODEL)


def test_generic_key_wins_over_legacy_key() -> None:
    env = {"LLM_PROVIDER": "groq", "LLM_API_KEY": "new", "GROQ_API_KEY": "old"}
    assert LLMConfig.from_env(env).api_key == "new"


def test_cli_flags_override_environment() -> None:
    env = {"LLM_PROVIDER": "groq", "LLM_API_KEY": "k", "LLM_MODEL": "a"}
    cfg = LLMConfig.from_env(env, provider="anthropic", model="b")
    assert (cfg.provider, cfg.model) == ("anthropic", "b")


@pytest.mark.parametrize(
    "env",
    [
        {},  # nothing configured
        {"LLM_PROVIDER": "nope", "LLM_API_KEY": "k"},  # unknown provider
        {"LLM_PROVIDER": "groq"},  # no key
        {"LLM_PROVIDER": "openai", "LLM_API_KEY": "k"},  # model required
        {
            "LLM_PROVIDER": "openai_compat",
            "LLM_API_KEY": "k",
            "LLM_MODEL": "m",
        },  # base url required
    ],
)
def test_misconfiguration_is_a_typed_error_without_secrets(env: dict[str, str]) -> None:
    with pytest.raises(LLMConfigError) as info:
        LLMConfig.from_env(env)
    assert str(info.value) != "k"


def test_build_llm_returns_the_right_adapter() -> None:
    groq = build_llm(LLMConfig.from_env({"GROQ_API_KEY": "k"}))
    compat = build_llm(
        LLMConfig.from_env(
            {
                "LLM_PROVIDER": "openai_compat",
                "LLM_API_KEY": "k",
                "LLM_MODEL": "m",
                "LLM_BASE_URL": "http://localhost:11434/v1",
            }
        )
    )
    anthropic = build_llm(LLMConfig.from_env({"ANTHROPIC_API_KEY": "k"}))
    assert isinstance(groq, OpenAICompatLLM) and isinstance(compat, OpenAICompatLLM)
    assert isinstance(anthropic, AnthropicLLM)
