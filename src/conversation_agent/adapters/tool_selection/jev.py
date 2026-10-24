"""`jev` provider: ranks tools with a hosted decision model (TypeSafe Jev).

One request, one `choice` question whose options are the candidate tools:

    POST {connection.base_url}{jev.path}          (default /v1/systemone)
    {"model": "jev-latest",
     "state": "<the user's message>",
     "questions": {"tool_pick": {"type": "choice", "instructions": "...",
                                 "criteria": {"<tool name>": "<description>", ...}}}}

    200 {"answers": {"tool_pick": {"type": "choice", "choice": "<tool name>",
                                   "confidence": 0.8, "probabilities": {"<tool name>": 0.8, ...}}}}

The probabilities sum to 1 over the options, so they are rescaled by the largest one (the best tool
scores 1.0): `min_score` then reads as "at least this fraction as likely as the best". A message
that needs two tools spreads the mass across both, which the rescaling keeps. The format comes from
the public OpenRouter/AIMLAPI documentation of the service; transport, credential, breaker and
failure handling are those of the `http` provider (`HttpToolSelector`).
"""

from __future__ import annotations

import json

from conversation_agent.adapters.tool_selection.http import HttpToolSelector, _Failure
from conversation_agent.core.definitions.tool_selection import JevSelectorSettings
from conversation_agent.core.models.llm import LLMToolDefinition

QUESTION = "tool_pick"


class JevToolSelector(HttpToolSelector):
    def _payload(self, message: str, candidates: tuple[LLMToolDefinition, ...]) -> bytes:
        settings = self._settings
        assert isinstance(settings, JevSelectorSettings)
        body = {
            "model": settings.model,
            "state": message,
            "questions": {
                QUESTION: {
                    "type": "choice",
                    "instructions": settings.instructions,
                    "criteria": {t.name: t.description for t in candidates},
                }
            },
        }
        return json.dumps(body, ensure_ascii=False).encode()

    def _scores(
        self, content: bytes, candidates: tuple[LLMToolDefinition, ...]
    ) -> dict[str, float]:
        try:
            answer = json.loads(content)["answers"][QUESTION]
            chosen = answer["choice"]
            raw = answer.get("probabilities")
        except (ValueError, KeyError, TypeError):
            raise _Failure("selector answer is not valid", health=True) from None
        sent = {t.name for t in candidates}
        if raw is None:  # only the winner is known
            raw = {chosen: 1.0}
        if not isinstance(raw, dict):
            raise _Failure("selector answer is not valid", health=True)
        probs: dict[str, float] = {}
        for name, p in raw.items():
            if name not in sent:
                continue
            if isinstance(p, bool) or not isinstance(p, int | float) or not 0.0 <= p <= 1.0:
                raise _Failure("selector answer is not valid", health=True)
            probs[name] = float(p)
        top = max(probs.values(), default=0.0)
        if top <= 0.0:
            raise _Failure("selector answer is not valid", health=True)
        return {name: p / top for name, p in probs.items()}
