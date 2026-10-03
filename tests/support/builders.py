from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

from conversation_agent.adapters.clock import FixedClock
from conversation_agent.adapters.journal.memory import InMemoryTurnJournal
from conversation_agent.adapters.llm.fake import tool_call_response
from conversation_agent.core.models.conversation import ConversationIdentity
from conversation_agent.core.models.llm import LLMRequest, LLMResponse, ToolResultPart

SP = ZoneInfo("America/Sao_Paulo")
# Monday 2026-10-05 08:00 in Sao Paulo: slots start at 09:00 the same day.
NOW = datetime(2026, 10, 5, 8, 0, tzinfo=SP)

IDENTITY = ConversationIdentity(
    tenant_id="tenant-1",
    channel_id="cli",
    conversation_id="conv-1",
    session_id="sess-1",
    contact_id="contact-1",
)


def new_clock() -> FixedClock:
    return FixedClock(NOW)


def new_journal() -> InMemoryTurnJournal:
    return InMemoryTurnJournal()


def last_tool_result(request: LLMRequest) -> dict[str, object]:
    """Parses the JSON the engine put in the most recent tool_result part."""
    for message in reversed(request.messages):
        for part in message.parts:
            if isinstance(part, ToolResultPart):
                parsed = json.loads(part.content)
                assert isinstance(parsed, dict)
                return parsed
    raise AssertionError("no tool_result in request")


def react(
    handler: Callable[[dict[str, object]], LLMResponse],
) -> Callable[[LLMRequest], LLMResponse]:
    """Scripted LLM step that decides based on the last tool result it was shown."""
    return lambda request: handler(last_tool_result(request))


def availability_call(service: str, day_from: str, day_to: str | None = None) -> LLMResponse:
    return tool_call_response(
        "scheduling__availability",
        {"service_id": service, "from_date": day_from, "to_date": day_to or day_from},
    )
