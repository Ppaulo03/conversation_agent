"""FlowRunner: executes Flows deterministically on top of the Capability pipeline (DESIGN §23).

The runner holds no state of its own: it takes the flow stack from `ConversationState`, returns
the new one, and does every external operation through `FlowIO` (the engine's journaled,
policy-gated, ledgered capability path). A flow therefore cannot do anything an LLM tool call
could not: it never touches a tool, an adapter or the database.

Progress is derived, not stored: after every user message the runner walks the steps in order
and acts on the first one whose inputs are not satisfied by the current slots/results. A
correction simply changes a slot; whatever depended on it stops matching and is redone, while
everything still valid is kept.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Protocol

from conversation_agent.core.canonical import stable_hash
from conversation_agent.core.definitions.agent import AgentDefinition
from conversation_agent.core.definitions.flow import (
    AddDays,
    Ask,
    Choose,
    Collect,
    FlowDefinition,
    Handoff,
    Invoke,
    Lit,
    Propose,
    Say,
    Slot,
    Table,
    Transition,
    expr_slots,
)
from conversation_agent.core.models.flow import FlowInstance, StepResult
from conversation_agent.core.temporal_ptbr import WEEKDAY_ABBREV, reference_date
from conversation_agent.engine.capability_pipeline import CapabilityOutcome
from conversation_agent.engine.flow_understanding import (
    build_options,
    contains_phrase,
    extract_slots,
    is_cancel,
    render_options,
    select_option,
)

_MAX_ROUNDS = 8  # a flow that cannot settle within this many internal re-scans is defective


class FlowIO(Protocol):
    """What the runner may ask of the engine: one capability call, with every guarantee
    (policy, journal, ledger) applied there."""

    async def invoke(self, capability: str, args: dict[str, Any]) -> CapabilityOutcome: ...


@dataclass(frozen=True)
class FlowTurn:
    text: str
    flows: tuple[FlowInstance, ...]
    reference_time: datetime
    io: FlowIO


@dataclass(frozen=True)
class FlowReply:
    reply: str
    flows: tuple[FlowInstance, ...]
    proposed: bool = False  # a protected action was proposed: the engine adds the question


@dataclass(frozen=True)
class _Moved:
    """Result of one pass over the steps."""

    flows: tuple[FlowInstance, ...]
    reply: str
    proposed: bool = False


@dataclass(frozen=True)
class _Next:
    flows: tuple[FlowInstance, ...]
    reply: (
        str | None
    )  # None: keep going (an Ask transition) with `preface` in front of the question
    preface: str


class _Slots(dict[str, Any]):
    def __missing__(self, key: str) -> str:
        return ""


class FlowRunner:
    def __init__(self, agent: AgentDefinition) -> None:
        self._agent = agent
        self._defs = {f.name: f for f in agent.flows}

    # ------------------------------------------------------------------ entry points

    async def handle(self, turn: FlowTurn) -> FlowReply | None:
        """A user message. None = no flow is involved (the normal agent loop answers)."""
        flows = list(turn.flows)
        active = flows[-1] if flows else None
        trigger = self._trigger(turn.text, active.flow_name if active else None)
        if active is None:
            return None if trigger is None else await self._start(flows, trigger, turn)
        definition = self._defs[active.flow_name]
        if is_cancel(turn.text):
            return self._cancel(flows, definition)
        today = reference_date(turn.reference_time, self._agent.timezone)
        answers = self._understand(definition, active, turn.text, today)[1]
        if trigger is not None and not answers:  # a different flow asked for: park this one
            flows[-1] = active.model_copy(update={"status": "suspended"})
            return await self._start(flows, trigger, turn)
        return await self._continue(flows, definition, active, turn)

    def after_action(
        self,
        flows: tuple[FlowInstance, ...],
        closed: str,
        status: str | None,
        reply: str,
    ) -> FlowReply:
        """The protected action the flow proposed reached a final state (executed / rejected /
        expired). The user already got the action's own message; the flow closes (or follows
        its transition when the execution failed) and a parked flow resumes."""
        active = flows[-1] if flows else None
        if active is None:
            return FlowReply(reply, flows)
        definition = self._defs[active.flow_name]
        if closed == "executed" and status not in (None, "success"):
            step = next((s for s in reversed(definition.steps) if isinstance(s, Propose)), None)
            if step is not None:
                transition = step.on.get(status, step.default)
                stack = list(flows)
                moved = self._transition(transition, stack, definition, active, "")
                if moved.reply is not None:
                    return FlowReply(moved.reply, moved.flows)
                if isinstance(transition, Ask):  # ask for the slot again, flow stays open
                    asked = self._ask(stack, definition, transition.slot, moved.preface)
                    return FlowReply(asked.reply, asked.flows)
        return self._finish(list(flows), reply)

    # ------------------------------------------------------------------ lifecycle

    def _trigger(self, text: str, current: str | None) -> FlowDefinition | None:
        for definition in self._defs.values():
            if definition.name != current and contains_phrase(text, definition.triggers):
                return definition
        return None

    async def _start(
        self, flows: list[FlowInstance], definition: FlowDefinition, turn: FlowTurn
    ) -> FlowReply:
        instance = FlowInstance(
            instance_id=stable_hash(definition.name, turn.text, turn.reference_time.isoformat())[
                :16
            ],
            flow_name=definition.name,
        )
        flows.append(instance)
        return await self._continue(flows, definition, instance, turn, understand=True)

    def _cancel(self, flows: list[FlowInstance], definition: FlowDefinition) -> FlowReply:
        return self._finish(flows, definition.cancelled_reply)

    def _finish(self, flows: list[FlowInstance], reply: str) -> FlowReply:
        """Drop the active flow; if one was parked, resume it where it stopped."""
        flows.pop()
        if flows:
            resumed = flows[-1].model_copy(update={"status": "active"})
            flows[-1] = resumed
            definition = self._defs[resumed.flow_name]
            line = definition.resume_reply.format(question=resumed.last_question)
            reply = f"{reply}\n\n{line}" if reply else line
        return FlowReply(reply, tuple(flows))

    # ------------------------------------------------------------------ one user message

    async def _continue(
        self,
        flows: list[FlowInstance],
        definition: FlowDefinition,
        instance: FlowInstance,
        turn: FlowTurn,
        *,
        understand: bool = False,
    ) -> FlowReply:
        today = reference_date(turn.reference_time, self._agent.timezone)
        instance, understood = self._understand(definition, instance, turn.text, today)
        flows[-1] = instance
        preface = ""
        asked = instance.awaiting_slot is not None or instance.awaiting_choice is not None
        if asked and not (understood or understand):  # nothing was asked: any message continues
            unclear = instance.unclear_count + 1
            if unclear > definition.max_unclear:
                return self._finish(flows, definition.giveup_reply)
            flows[-1] = instance.model_copy(update={"unclear_count": unclear})
            return FlowReply(
                f"{definition.unclear_reply} {instance.last_question}".strip(), tuple(flows)
            )
        flows[-1] = flows[-1].model_copy(update={"unclear_count": 0})
        moved = await self._advance(flows, definition, turn, preface)
        return FlowReply(moved.reply, moved.flows, moved.proposed)

    def _understand(
        self, definition: FlowDefinition, instance: FlowInstance, text: str, today: date
    ) -> tuple[FlowInstance, bool]:
        slots = dict(instance.slots)
        if instance.awaiting_choice is not None:
            step = next(s for s in definition.steps if s.id == instance.awaiting_choice)
            assert isinstance(step, Choose)
            picked = select_option(text, instance.options.get(step.id, []), today)
            if picked is not None:
                slots[step.into] = picked["value"]
                return instance.model_copy(update={"slots": slots, "awaiting_choice": None}), True
        understood = False
        for name, value in extract_slots(definition, text, today, instance.awaiting_slot).items():
            understood = True
            if slots.get(name) != value:
                slots[name] = value
                for derived in definition.slot(name).invalidates:  # correction: forget what
                    slots.pop(derived, None)  # was derived from the old value
        return instance.model_copy(update={"slots": slots}), understood

    # ------------------------------------------------------------------ progress

    async def _advance(
        self,
        flows: list[FlowInstance],
        definition: FlowDefinition,
        turn: FlowTurn,
        preface: str,
    ) -> _Moved:
        for _ in range(_MAX_ROUNDS):
            instance = flows[-1]
            settled = False
            for step in definition.steps:
                instance = flows[-1]  # steps above may have updated slots/results
                if isinstance(step, Collect):
                    missing = [
                        n
                        for n in step.slots
                        if definition.slot(n).required and n not in instance.slots
                    ]
                    if missing:
                        return self._ask(flows, definition, missing[0], preface)
                    continue
                if isinstance(step, Invoke | Propose):
                    absent = self._unset_input(step, instance.slots)
                    if absent is not None:
                        return self._ask(flows, definition, absent, preface)
                    args = self._evaluate(step.inputs, instance.slots)
                    digest = stable_hash(step.capability, args)
                    if isinstance(step, Invoke):
                        known = instance.results.get(step.id)
                        if known is not None and known.input_hash == digest:
                            continue
                        outcome = await turn.io.invoke(step.capability, args)
                        result = outcome.result
                        assert result is not None
                        if result.status == "success":
                            flows[-1] = instance.model_copy(
                                update={
                                    "results": {
                                        **instance.results,
                                        step.id: StepResult(
                                            input_hash=digest,
                                            status="success",
                                            data=result.data,
                                        ),
                                    }
                                }
                            )
                            instance = flows[-1]
                            continue
                        transition = step.on.get(result.status, step.default)
                    else:
                        outcome = await turn.io.invoke(step.capability, args)
                        if outcome.proposal is not None:
                            flows[-1] = instance.model_copy(
                                update={
                                    "proposal_hash": digest,
                                    "awaiting_slot": None,
                                    "awaiting_choice": None,
                                    "last_question": step.intro,
                                }
                            )
                            return _Moved(tuple(flows), step.intro, proposed=True)
                        status = outcome.result.status if outcome.result else "unknown"
                        transition = step.on.get(status, step.default)
                    moved = self._transition(transition, flows, definition, instance, preface)
                    if moved.reply is not None:
                        return _Moved(moved.flows, moved.reply)
                    flows, preface, settled = list(moved.flows), moved.preface, True
                    break
                if isinstance(step, Choose):
                    outcome_c = self._choose(flows, definition, instance, step, preface)
                    if outcome_c is None:
                        continue  # satisfied (maybe auto-selected): look at the next step
                    if isinstance(outcome_c, _Moved):
                        return outcome_c
                    flows, preface = outcome_c
                    settled = True
                    break
            if not settled:
                return _Moved(*self._complete(flows, definition))
        return _Moved(tuple(flows), self._agent.fallback_reply)

    def _complete(
        self, flows: list[FlowInstance], definition: FlowDefinition
    ) -> tuple[tuple[FlowInstance, ...], str]:
        done = self._finish(flows, definition.completed_reply)
        return done.flows, done.reply

    def _ask(
        self, flows: list[FlowInstance], definition: FlowDefinition, slot: str, preface: str
    ) -> _Moved:
        question = definition.slot(slot).prompt.format_map(_Slots(self._display(flows[-1])))
        flows[-1] = flows[-1].model_copy(
            update={"awaiting_slot": slot, "awaiting_choice": None, "last_question": question}
        )
        return _Moved(tuple(flows), f"{preface}{question}")

    def _choose(
        self,
        flows: list[FlowInstance],
        definition: FlowDefinition,
        instance: FlowInstance,
        step: Choose,
        preface: str,
    ) -> _Moved | tuple[list[FlowInstance], str] | None:
        source = instance.results[step.source]
        raw = (source.data or {}).get(step.list_field) or []
        items = [i for i in raw if isinstance(i, dict)][: step.max_options]
        if not items:
            moved = self._transition(step.empty, flows, definition, instance, preface)
            if moved.reply is not None:
                return _Moved(moved.flows, moved.reply)
            return list(moved.flows), moved.preface
        options = build_options(items, step.value_field, self._agent.timezone)
        if instance.slots.get(step.into) in [o["value"] for o in options]:
            return None  # still valid for the current search
        wanted_time = instance.slots.get(step.prefer_time_slot) if step.prefer_time_slot else None
        wanted_date = instance.slots.get(step.prefer_date_slot) if step.prefer_date_slot else None
        stated = wanted_time is not None or wanted_date is not None
        if stated:
            matches = [
                o
                for o in options
                if (wanted_time is None or o["local_time"] == wanted_time)
                and (wanted_date is None or o["local_date"] == wanted_date)
            ]
            if len(matches) == 1:
                flows[-1] = instance.model_copy(
                    update={"slots": {**instance.slots, step.into: matches[0]["value"]}}
                )
                return None
        lead = f"{step.no_match_text} " if stated and step.no_match_text else ""
        question = step.prompt.replace("{options}", render_options(options)).format_map(
            _Slots(self._display(instance))
        )
        flows[-1] = instance.model_copy(
            update={
                "awaiting_choice": step.id,
                "awaiting_slot": None,
                "options": {**instance.options, step.id: options},
                "last_question": question,
            }
        )
        return _Moved(tuple(flows), f"{preface}{lead}{question}")

    # ------------------------------------------------------------------ transitions

    def _transition(
        self,
        transition: Transition,
        flows: list[FlowInstance],
        definition: FlowDefinition,
        instance: FlowInstance,
        preface: str,
    ) -> _Next:
        text = transition.text if transition.text is not None else ""
        spoken = text.format_map(_Slots(self._display(instance)))
        if isinstance(transition, Say):
            if transition.end:
                done = self._finish(flows, spoken)
                return _Next(done.flows, done.reply, "")
            return _Next(tuple(flows), spoken, "")
        if isinstance(transition, Handoff):
            done = self._finish(flows, spoken)
            return _Next(done.flows, done.reply, "")
        assert isinstance(transition, Ask)
        slots = {k: v for k, v in instance.slots.items() if k != transition.slot}
        for derived in definition.slot(transition.slot).invalidates:
            slots.pop(derived, None)
        flows[-1] = instance.model_copy(update={"slots": slots})
        lead = f"{spoken}\n" if spoken else ""
        return _Next(tuple(flows), None, f"{preface}{lead}")

    # ------------------------------------------------------------------ expressions

    def _unset_input(self, step: Invoke | Propose, slots: dict[str, Any]) -> str | None:
        for expr in step.inputs.values():
            for name in expr_slots(expr):
                if name not in slots:
                    return name
        return None

    def _evaluate(self, inputs: dict[str, Any], slots: dict[str, Any]) -> dict[str, Any]:
        return {name: self._value(expr, slots) for name, expr in inputs.items()}

    def _value(self, expr: Slot | Lit | AddDays | Table, slots: dict[str, Any]) -> Any:
        if isinstance(expr, Slot):
            return slots[expr.name]
        if isinstance(expr, Lit):
            return expr.value
        if isinstance(expr, AddDays):
            return (
                date.fromisoformat(slots[expr.base.name]) + timedelta(days=expr.days)
            ).isoformat()
        return expr.mapping[slots[expr.key.name]]

    def _display(self, instance: FlowInstance) -> dict[str, Any]:
        """Slot values as people write them (dates as 'ter 06/10')."""
        shown: dict[str, Any] = {}
        for name, value in instance.slots.items():
            if isinstance(value, str) and len(value) == 10 and value[4:5] == "-":
                try:
                    day = date.fromisoformat(value)
                    shown[name] = f"{WEEKDAY_ABBREV[day.weekday()]} {day.day:02d}/{day.month:02d}"
                    continue
                except ValueError:
                    pass
            shown[name] = value
        return shown
