"""Generic multi-turn CLI loop (inbound adapter for local use). Domain-agnostic."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable

from conversation_agent.core.errors import ConversationAgentError
from conversation_agent.core.models.conversation import ConversationIdentity, ConversationState
from conversation_agent.engine.turn_engine import TurnEngine


async def run_cli(
    engine: TurnEngine,
    identity: ConversationIdentity,
    *,
    read_line: Callable[[str], str] = input,
    write: Callable[[str], None] = print,
) -> ConversationState:
    """Reads user lines until `/quit`/EOF. `/state` prints the conversational state."""
    state = ConversationState()
    write("Type a message. Commands: /state, /quit")
    while True:
        try:
            line = (await asyncio.to_thread(read_line, "you> ")).strip()
        except EOFError:
            break
        if not line:
            continue
        if line == "/quit":
            break
        if line == "/state":
            write(json.dumps(state.model_dump(mode="json"), ensure_ascii=False, indent=2))
            continue
        try:
            outcome = await engine.process_turn(
                identity, state, line, turn_id=f"cli-{uuid.uuid4().hex[:12]}"
            )
        except ConversationAgentError as exc:
            write(f"[error] {type(exc).__name__}: {exc}")
            continue
        state = outcome.state
        write(f"bot> {outcome.reply}")
    return state
