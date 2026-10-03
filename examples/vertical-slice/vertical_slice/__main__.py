"""`python -m vertical_slice`: scheduling agent on the CLI with a real LLM provider."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from conversation_agent.adapters.llm.anthropic import AnthropicLLM
from conversation_agent.app.cli import run_cli
from conversation_agent.core.models.conversation import ConversationIdentity
from vertical_slice.wiring import build_engine


async def main() -> int:
    parser = argparse.ArgumentParser(prog="vertical_slice")
    parser.add_argument("--api-url", default="http://127.0.0.1:8001")
    parser.add_argument("--model", default="claude-sonnet-5-5")
    args = parser.parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("Set ANTHROPIC_API_KEY to use the real provider.", file=sys.stderr)
        return 2

    llm = AnthropicLLM.from_api_key(api_key, args.model)
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
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
