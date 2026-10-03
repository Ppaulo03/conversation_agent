"""`python -m vertical_slice`: scheduling agent on the CLI with a real LLM provider.

Configuration comes from the environment (see conversation_agent.app.llm_factory):
LLM_PROVIDER / LLM_API_KEY / LLM_MODEL / LLM_BASE_URL, or the legacy GROQ_API_KEY /
ANTHROPIC_API_KEY. Run with `uv run --env-file .env python -m vertical_slice`.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from conversation_agent.app.cli import run_cli
from conversation_agent.app.llm_factory import LLMConfig, LLMConfigError, build_llm, close_llm
from conversation_agent.core.models.conversation import ConversationIdentity
from vertical_slice.wiring import build_engine


async def main() -> int:
    parser = argparse.ArgumentParser(prog="vertical_slice")
    parser.add_argument("--api-url", default="http://127.0.0.1:8001")
    parser.add_argument("--provider", choices=["groq", "openai", "openai_compat", "anthropic"])
    parser.add_argument("--model", default=None)
    args = parser.parse_args()

    try:
        config = LLMConfig.from_env(os.environ, provider=args.provider, model=args.model)
    except LLMConfigError as exc:
        print(f"LLM configuration error: {exc}", file=sys.stderr)
        return 2
    llm = build_llm(config)
    print(f"[llm] provider={config.provider} model={config.model}")

    engine, _, http = build_engine(llm, api_base_url=args.api_url)
    identity = ConversationIdentity(
        tenant_id="local-tenant",
        channel_id="cli",
        conversation_id="cli-conversation",
        session_id="cli-session",
        contact_id="cli-contact",
    )
    try:
        await run_cli(engine, identity)
    finally:
        if http is not None:
            await http.aclose()
        await close_llm(llm)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
