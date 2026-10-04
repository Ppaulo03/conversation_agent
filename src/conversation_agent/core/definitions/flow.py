"""Flows: typed conversational state machines (DESIGN §23).

A Flow is *conversation*, not integration: it collects slots, invokes Capabilities (never tools
or adapters), lets the user choose among real results, asks for confirmation, and says what to
do for every canonical outcome. Built from typed Python objects first; a YAML serialisation
comes only in Phase 5, once this model is proven.

Progress is *derived from state*, not stored as a cursor: each step knows whether it is
satisfied by the current slots/results. That is what makes corrections cheap and safe:
changing a slot makes every step that depended on it unsatisfied again (and clears the slots
derived from it), while everything still valid is kept.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, model_validator

from conversation_agent.core.errors import DefinitionError
from conversation_agent.core.models.tooling import ToolStatus

SlotType = Literal["enum", "date", "time", "duration_minutes", "text"]
# Every non-success outcome must lead somewhere safe (DESIGN §23.4). `success` simply continues.
NON_SUCCESS_STATUSES: tuple[str, ...] = tuple(s for s in get_args(ToolStatus) if s != "success")


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SlotDefinition(_Frozen):
    name: str
    type: SlotType
    prompt: str  # what to ask when this slot is needed
    choices: dict[str, tuple[str, ...]] = Field(default_factory=dict)  # enum: canonical -> synonyms
    required: bool = True
    # Slots derived from this one: when its value CHANGES they are forgotten (correction).
    invalidates: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _enum_needs_choices(self) -> SlotDefinition:
        if self.type == "enum" and not self.choices:
            raise ValueError(f"enum slot {self.name!r} needs `choices`")
        return self


# --- expressions: how a step builds capability arguments from slots ---
class Slot(_Frozen):
    kind: Literal["slot"] = "slot"
    name: str


class Lit(_Frozen):
    kind: Literal["lit"] = "lit"
    value: Any


class AddDays(_Frozen):
    """An ISO date `days` after a date slot (e.g. the end of a search window)."""

    kind: Literal["add_days"] = "add_days"
    base: Slot
    days: int


class Table(_Frozen):
    """Look a slot's value up in a fixed table (e.g. service -> duration)."""

    kind: Literal["table"] = "table"
    key: Slot
    mapping: dict[str, Any]


Expr = Annotated[Slot | Lit | AddDays | Table, Field(discriminator="kind")]


def expr_slots(expr: Slot | Lit | AddDays | Table) -> tuple[str, ...]:
    if isinstance(expr, Slot):
        return (expr.name,)
    if isinstance(expr, AddDays):
        return (expr.base.name,)
    if isinstance(expr, Table):
        return (expr.key.name,)
    return ()


# --- transitions ---
class Say(_Frozen):
    """Reply with `text`. `end=True` closes the flow; otherwise it stays open and the same step
    is retried when the user writes again (a natural "try again")."""

    kind: Literal["say"] = "say"
    text: str
    end: bool = False


class Ask(_Frozen):
    """Forget `slot` and ask for it again (e.g. no availability that day -> ask for another)."""

    kind: Literal["ask"] = "ask"
    slot: str
    text: str | None = None  # preface, shown before the slot's own prompt


class Handoff(_Frozen):
    """Give up on automation for this flow and say so (ownership change is a channel concern)."""

    kind: Literal["handoff"] = "handoff"
    text: str


Transition = Annotated[Say | Ask | Handoff, Field(discriminator="kind")]


# --- steps ---
class Collect(_Frozen):
    kind: Literal["collect"] = "collect"
    id: str
    slots: tuple[str, ...]


class Invoke(_Frozen):
    """Read capability. Satisfied while its recorded result matches its CURRENT inputs."""

    kind: Literal["invoke"] = "invoke"
    id: str
    capability: str
    inputs: dict[str, Expr]
    on: dict[str, Transition] = Field(default_factory=dict)  # non-success status -> transition
    default: Transition  # required: the safe answer for any outcome not listed in `on`


class Choose(_Frozen):
    """Let the user pick one of the REAL items a previous Invoke returned (never invented)."""

    kind: Literal["choose"] = "choose"
    id: str
    source: str  # id of an earlier Invoke
    list_field: str  # field of that result holding the list
    value_field: str  # field of each item that becomes the slot value
    into: str  # slot receiving the chosen value
    prompt: str  # `{options}` is replaced by the numbered list
    empty: Transition  # nothing came back
    max_options: int = 5
    prefer_time_slot: str | None = None  # a time the user already stated: auto-select if unique
    prefer_date_slot: str | None = None


class Propose(_Frozen):
    """Protected capability: only *proposes*; the PolicyGate/PendingAction machinery asks for the
    explicit confirmation and executes (Phase 3)."""

    kind: Literal["propose"] = "propose"
    id: str
    capability: str
    inputs: dict[str, Expr]
    on: dict[str, Transition] = Field(default_factory=dict)
    default: Transition


Step = Annotated[Collect | Invoke | Choose | Propose, Field(discriminator="kind")]


class FlowDefinition(_Frozen):
    name: str
    description: str = ""
    triggers: tuple[str, ...] = ()  # whole words/phrases that start this flow (folded, no accents)
    slots: tuple[SlotDefinition, ...]
    steps: tuple[Step, ...]
    cancelled_reply: str = "Ok, cancelei."
    completed_reply: str | None = None
    resume_reply: str = "Voltando ao que estávamos fazendo: {question}"
    digressions_allowed: bool = True
    digression_capabilities: tuple[str, ...] = ()  # read capabilities usable in a digression

    @model_validator(mode="after")
    def _consistent(self) -> FlowDefinition:
        slot_names = [s.name for s in self.slots]
        step_ids = [s.id for s in self.steps]
        for label, names in (("slot", slot_names), ("step", step_ids)):
            if len(set(names)) != len(names):
                raise DefinitionError(f"flow {self.name!r}: duplicate {label} names")
        known = set(slot_names)

        def need(slot: str, where: str) -> None:
            if slot not in known:
                raise DefinitionError(
                    f"flow {self.name!r}: {where} refers to unknown slot {slot!r}"
                )

        for slot in self.slots:
            for derived in slot.invalidates:
                need(derived, f"slot {slot.name!r}.invalidates")
        seen_invokes: set[str] = set()
        for step in self.steps:
            if isinstance(step, Collect):
                for name in step.slots:
                    need(name, f"collect {step.id!r}")
            if isinstance(step, Invoke | Propose):
                for expr in step.inputs.values():
                    for name in expr_slots(expr):
                        need(name, f"step {step.id!r} inputs")
                bad = set(step.on) - set(NON_SUCCESS_STATUSES)
                if bad:
                    raise DefinitionError(
                        f"flow {self.name!r}: step {step.id!r} maps unknown outcomes {sorted(bad)}"
                    )
                for transition in (*step.on.values(), step.default):
                    self._check_transition(transition, known)
            if isinstance(step, Invoke):
                seen_invokes.add(step.id)
            if isinstance(step, Choose):
                if step.source not in seen_invokes:
                    raise DefinitionError(
                        f"flow {self.name!r}: choose {step.id!r} needs an EARLIER invoke "
                        f"{step.source!r}"
                    )
                need(step.into, f"choose {step.id!r}.into")
                for ref in (step.prefer_time_slot, step.prefer_date_slot):
                    if ref is not None:
                        need(ref, f"choose {step.id!r}")
                self._check_transition(step.empty, known)
        return self

    def _check_transition(self, transition: Say | Ask | Handoff, known: set[str]) -> None:
        if isinstance(transition, Ask) and transition.slot not in known:
            raise DefinitionError(
                f"flow {self.name!r}: ask transition refers to unknown slot {transition.slot!r}"
            )

    def slot(self, name: str) -> SlotDefinition:
        return next(s for s in self.slots if s.name == name)
