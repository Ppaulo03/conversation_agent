"""Eval scenarios as data (DESIGN §43): deterministic assertions on a conversation.

A scenario is a list of turns. Each turn has the user message, optionally the scripted model
steps for that turn, and what must be true afterwards. The critical invariants are assertions
(this capability was NEVER executed, this action was proposed and NOT executed), never a model's
opinion. Scenarios are portable: the same file runs against an agent whose tools are served by a
real API, a recorded cassette or a fake.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from conversation_agent.core.canonical import stable_hash


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)


class ToolCallStep(_Strict):
    name: str  # the name the MODEL sees (capability name with dots as `__`)
    arguments: dict[str, Any] = Field(default_factory=dict)


class LLMStep(_Strict):
    """One scripted model answer, exactly one of: `text`, `tool_call`, `structured`."""

    text: str | None = None
    tool_call: ToolCallStep | None = None
    structured: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> LLMStep:
        given = [v for v in (self.text, self.tool_call, self.structured) if v is not None]
        if len(given) != 1:
            raise ValueError("an LLM step is exactly one of text, tool_call, structured")
        return self


class EvalTurn(_Strict):
    """One user message and what must be true of the turn. `capture` stores a regex match of the
    reply (group 1, else the whole match): `{name}` in a later `user` is replaced by it."""

    user: str = ""
    # The contact sent a voice message whose transcript is exactly this (scripted: no audio, no
    # provider). Whether the agent hears it depends on its `transcription` setting, which is the
    # point: a suite can prove both what the agent does with the words and that `off` ignores them.
    voice: str | None = None
    llm: tuple[LLMStep, ...] = ()  # scripted model answers this turn consumes, in order
    reply_contains: tuple[str, ...] = ()
    reply_not_contains: tuple[str, ...] = ()
    reply_matches: tuple[str, ...] = ()
    capture: dict[str, str] = Field(default_factory=dict)
    proposes: str | None = None  # exactly this capability is left proposed (never executed)
    proposes_nothing: bool = False
    executes: tuple[str, ...] = ()  # capabilities that must have really been executed
    executes_nothing: bool = False
    forbidden_capabilities: tuple[str, ...] = ()  # never proposed and never executed this turn
    flow: str | None = None  # the flow that must be active afterwards ("none": no flow)
    handoff: bool | None = None  # the turn must (not) hand the conversation to a person
    max_llm_calls: int | None = Field(default=None, ge=0)

    @field_validator("reply_matches")
    @classmethod
    def _valid_regexes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for pattern in value:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"{pattern!r} is not a valid regular expression: {exc}") from exc
        return value

    @model_validator(mode="after")
    def _consistent(self) -> EvalTurn:
        if not self.user and self.voice is None:
            raise ValueError("a turn needs a `user` message or a `voice` transcript")
        if self.proposes is not None and self.proposes_nothing:
            raise ValueError("a turn cannot both propose and propose nothing")
        if self.executes and self.executes_nothing:
            raise ValueError("a turn cannot both execute and execute nothing")
        overlap = set(self.forbidden_capabilities) & ({self.proposes} | set(self.executes))
        if overlap - {None}:
            raise ValueError(f"{sorted(x for x in overlap if x)} is both expected and forbidden")
        return self


class EvalScenario(_Strict):
    name: str
    description: str = ""
    turns: tuple[EvalTurn, ...] = Field(min_length=1)


class EvalSuite(_Strict):
    """A named set of scenarios plus the variables their `{name}` placeholders need."""

    suite: str
    description: str = ""
    variables: dict[str, str] = Field(default_factory=dict)
    scenarios: tuple[EvalScenario, ...] = Field(min_length=1)

    @field_validator("scenarios")
    @classmethod
    def _unique_names(cls, value: tuple[EvalScenario, ...]) -> tuple[EvalScenario, ...]:
        names = [s.name for s in value]
        if len(set(names)) != len(names):
            raise ValueError("scenario names must be unique")
        return value

    @property
    def digest(self) -> str:
        document = self.model_dump(mode="json")
        for scenario in document["scenarios"]:
            for turn in scenario["turns"]:
                if turn.get("voice") is None:  # only turns that use it carry it: a suite written
                    turn.pop(
                        "voice", None
                    )  # before voice turns keeps the digest it was released with
        return stable_hash(document)
