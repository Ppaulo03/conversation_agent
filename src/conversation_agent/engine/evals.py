"""Running eval scenarios against an engine (DESIGN §43).

The runner drives the REAL engine turn by turn, carrying conversation state like the coordinator
would, and reports every expectation that failed (it does not stop at the first one). It judges
what the agent SAID, PROPOSED and EXECUTED; what an external system ended up holding is that
system's business, checked by whoever owns it.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from conversation_agent.core.definitions.evals import EvalScenario, EvalTurn
from conversation_agent.core.models.conversation import (
    ConversationIdentity,
    ConversationState,
    TurnOutcome,
)
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
    executed: Callable[[], list[str]] = list,
) -> EvalResult:
    """`variables` fill the `{name}` placeholders only the installing agent can know; `executed`
    returns, in order, the capabilities that have really reached a provider so far."""
    result = EvalResult(scenario.name)
    state = ConversationState()
    captured: dict[str, str] = dict(variables or {})
    for index, turn in enumerate(scenario.turns, start=1):
        user = _PLACEHOLDER.sub(lambda m: captured.get(m.group(1), m.group(0)), turn.user)
        where = f"{scenario.name} turn {index}"
        before = len(executed())
        try:
            outcome = await engine.process_turn(identity, state, user, f"{turn_prefix}-{index}")
        except Exception as exc:  # e.g. the scripted model was called when the flow should answer
            result.failures.append(f"{where}: the turn failed: {type(exc).__name__}: {exc}")
            return result
        state = outcome.state
        result.transcript.append((user, outcome.reply))
        result.failures += check_turn(turn, outcome, executed()[before:], where)
        for name, pattern in turn.capture.items():
            match = re.search(pattern, outcome.reply)
            if match is None:
                result.failures.append(f"{where}: nothing to capture for {name!r} ({pattern!r})")
            else:
                captured[name] = match.group(1) if match.groups() else match.group(0)
    return result


def check_turn(turn: EvalTurn, outcome: TurnOutcome, ran: list[str], where: str) -> list[str]:
    reply = outcome.reply
    proposed = [p.request.capability for p in outcome.proposed]
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
    failures += [
        f"{where}: expected {name!r} to be executed, executed {ran}"
        for name in turn.executes
        if name not in ran
    ]
    if turn.executes_nothing and ran:
        failures.append(f"{where}: expected nothing executed, executed {ran}")
    for name in turn.forbidden_capabilities:
        if name in ran or name in proposed:
            failures.append(f"{where}: forbidden capability {name!r} was used")
    if turn.flow is not None:
        active = outcome.state.active_flow
        actual = active.flow_name if active is not None else "none"
        if actual != turn.flow:
            failures.append(f"{where}: expected flow {turn.flow!r}, found {actual!r}")
    if turn.handoff is not None and outcome.handoff_requested != turn.handoff:
        failures.append(
            f"{where}: expected handoff={turn.handoff}, got {outcome.handoff_requested}"
        )
    if turn.max_llm_calls is not None and outcome.llm_calls > turn.max_llm_calls:
        failures.append(
            f"{where}: {outcome.llm_calls} model calls, at most {turn.max_llm_calls} allowed"
        )
    return failures
