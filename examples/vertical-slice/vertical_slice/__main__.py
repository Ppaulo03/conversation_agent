"""`python -m vertical_slice`: scheduling agent on the CLI with a real LLM provider."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from conversation_agent.adapters.llm.anthropic import AnthropicLLM
from conversation_agent.adapters.llm.openai_compat import GROQ_DEFAULT_MODEL, OpenAICompatLLM
from conversation_agent.app.cli import run_cli
from conversation_agent.core.models.conversation import ConversationIdentity
from conversation_agent.ports.llm import LLMProvider
from vertical_slice.wiring import build_engine

ANTHROPIC_DEFAULT_MODEL = "claude-sonnet-5-5"


def build_llm(provider: str, model: str | None) -> LLMProvider | None:
    """Keys come from the environment only (never from flags or files)."""
    if provider == "groq":
        key = os.environ.get("GROQ_API_KEY")
        return OpenAICompatLLM.groq(key, model or GROQ_DEFAULT_MODEL) if key else None
    key = os.environ.get("ANTHROPIC_API_KEY")
    return AnthropicLLM.from_api_key(key, model or ANTHROPIC_DEFAULT_MODEL) if key else None


async def main() -> int:
    parser = argparse.ArgumentParser(prog="vertical_slice")
    parser.add_argument("--api-url", default="http://127.0.0.1:8001")
    parser.add_argument("--provider", choices=["groq", "anthropic"], default="groq")
    parser.add_argument("--model", default=None)
    args = parser.parse_args()

    llm = build_llm(args.provider, args.model)
    if llm is None:
        env = "GROQ_API_KEY" if args.provider == "groq" else "ANTHROPIC_API_KEY"
        print(f"Set {env} to use the {args.provider} provider.", file=sys.stderr)
        return 2

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
        if isinstance(llm, OpenAICompatLLM):
            await llm.aclose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
