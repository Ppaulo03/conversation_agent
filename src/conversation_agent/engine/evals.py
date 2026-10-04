"""Running a Pack's eval scenarios against a compiled agent (DESIGN §43).

A scenario is data: user messages and what must be true of each answer. The runner drives the
REAL engine turn by turn, carrying conversation state like the coordinator would, and reports
every expectation that failed (it does not stop at the first one). It judges what the agent SAID
and PROPOSED; what an external system ended up holding is that system's business, checked by
whoever owns it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from conversation_agent.core.definitions.pack import EvalScenario, EvalTurn
from conversation_agent.core.models.conversation import ConversationIdentity, ConversationState
from conversation_agent.engine.turn_engine import TurnEngine

_PLACEHOLDER = re.compile(r"\{(\w+)\}")


@dataclass
class EvalResult:
    scenario: str
    failures: list[str] = field(default_factory=list)
    transcript: list[tuple[str, str]] = field(default_factory=list)  # (user, reply)

    @property
    def passed(self) -> bool:
        return not self.failures


async def run_scenario(
    scenario: EvalScenario,
    engine: TurnEngine,
    identity: ConversationIdentity,
    *,
    turn_prefix: str = "eval",
    variables: dict[str, str] | None = None,
) -> EvalResult:
    """`variables` fill the `{name}` placeholders only the installing agent can know."""
    result = EvalResult(scenario.name)
    state = ConversationState()
    captured: dict[str, str] = dict(variables or {})
    for index, turn in enumerate(scenario.turns, start=1):
        user = _PLACEHOLDER.sub(lambda m: captured.get(m.group(1), m.group(0)), turn.user)
        where = f"{scenario.name} turn {index}"
        try:
            outcome = await engine.process_turn(identity, state, user, f"{turn_prefix}-{index}")
        except Exception as exc:  # e.g. the scripted model was called when the flow should answer
            result.failures.append(f"{where}: the turn failed: {type(exc).__name__}: {exc}")
            return result
        state = outcome.state
        result.transcript.append((user, outcome.reply))
        proposed = [p.request.capability for p in outcome.proposed]
        result.failures += _check(turn, outcome.reply, proposed, where)
        for name, pattern in turn.capture.items():
            match = re.search(pattern, outcome.reply)
            if match is None:
                result.failures.append(f"{where}: nothing to capture for {name!r} ({pattern!r})")
            else:
                captured[name] = match.group(1) if match.groups() else match.group(0)
    return result


def _check(turn: EvalTurn, reply: str, proposed: list[str], where: str) -> list[str]:
    failures = [f"{where}: reply lacks {t!r}" for t in turn.reply_contains if t not in reply]
    failures += [f"{where}: reply contains {t!r}" for t in turn.reply_not_contains if t in reply]
    failures += [
        f"{where}: reply does not match {pattern!r}"
        for pattern in turn.reply_matches
        if re.search(pattern, reply) is None
    ]
    if turn.proposes is not None and proposed != [turn.proposes]:
        failures.append(f"{where}: expected to propose {turn.proposes!r}, proposed {proposed}")
    if turn.proposes_nothing and proposed:
        failures.append(f"{where}: expected no proposal, proposed {proposed}")
    return failures
