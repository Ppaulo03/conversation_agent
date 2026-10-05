"""AgentDefinition: built directly from typed Python objects (DESIGN §3, ROADMAP Phase 1).

No Pack and no YAML/DSL: those arrive only after this model is proven.
"""

from __future__ import annotations

from typing import Literal, get_args, get_origin
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, model_validator

from conversation_agent.core.definitions.binding import CapabilityBinding, ResolvedToolBinding
from conversation_agent.core.definitions.capability import RISK_ORDER, CapabilityDefinition
from conversation_agent.core.definitions.flow import Choose, FlowDefinition, Invoke, Propose
from conversation_agent.core.definitions.schema_spec import schema_fingerprint
from conversation_agent.core.definitions.tool import ToolDefinition
from conversation_agent.core.errors import DefinitionError
from conversation_agent.core.templates import check_template, placeholders
from conversation_agent.core.temporal_ptbr import fold


class HumanRequest(BaseModel):
    """The contact asks for a person ("quero falar com um atendente"). Matched by RULES on short
    messages (a long message that merely mentions an attendant is not a request), never negated
    ("não quero falar com atendente"), and answered by handing the conversation over: the reply
    below is said, the conversation moves to HANDOFF_PENDING, flows are closed and the bot goes
    silent until a person returns it (INV-019). Nothing here is decided by a model.

    Optional: an agent without the block treats such a message like any other."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    triggers: tuple[str, ...]
    reply: str
    max_words: int = 12
    # False: this agent has NO person behind it. The same triggers still match, and `reply` is said
    # (an honest "I cannot call anyone here"), but the conversation does NOT change owner and open
    # flows are left alone: the model never improvises a promise the system cannot keep.
    available: bool = True

    @model_validator(mode="after")
    def _usable(self) -> HumanRequest:
        if not self.triggers or any(len(fold(t).split()) < 2 for t in self.triggers):
            raise ValueError("a human request needs triggers of at least two words each")
        if not self.reply.strip():
            raise ValueError("a human request needs a reply")
        if self.max_words < 2:
            raise ValueError("max_words must be at least 2")
        return self


class ConfirmationTexts(BaseModel):
    """Deterministic, runtime-owned wording for the confirmation protocol (one language per
    agent). `{summary}` is the rendered action summary."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt: str = "Please confirm: {summary}. Reply YES to confirm or NO to cancel."
    reprompt: str = "I did not catch that. {summary} - reply YES to confirm or NO to cancel."
    # Said instead of `reprompt` when the contact DID say yes but the runtime cannot prove it
    # answers this prompt (no reply-to, clocks too close or unknown, prompt not yet accepted):
    # the contact is told why, and how to make it count. None: `reprompt` is used.
    reprompt_unproven: str | None = None
    rejected: str = "Understood, I cancelled that request."
    expired: str = "That request expired. Tell me again what you would like to do."
    gave_up: str = "I could not get a clear confirmation, so I cancelled the request."
    executed_fallback: str = "Your request was processed (status: {status})."


class MediaTexts(BaseModel):
    """Wording the runtime puts in front of the agent when the contact sends media. Transcribed
    audio becomes `{audio}`; media the runtime cannot read is only NAMED, never invented."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    audio: str = "[voice message] {text}"
    audio_failed: str = "[voice message that could not be transcribed]"
    # Said when this agent does not transcribe (`transcription: off`, the default): the audio is
    # named, never sent anywhere, never pretended to have been heard.
    audio_disabled: str = "[voice message received; this assistant does not process voice messages]"
    image: str = "[image received]"
    video: str = "[video received]"
    document: str = "[document received: {name}]"
    too_large: str = "[media too large to process]"
    unsupported: str = "[media of a type that is not supported]"
    unavailable: str = "[media that could not be retrieved; ask the contact to send it again]"


class AgentDefinition(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    agent_id: str
    version: str
    persona: str  # domain prompt text; generic runtime rules are added by the engine
    timezone: str = "UTC"
    capabilities: tuple[CapabilityDefinition, ...]
    tools: tuple[ToolDefinition, ...]
    bindings: tuple[CapabilityBinding, ...]
    allowed_capabilities: frozenset[str]
    max_history_messages: int = 40
    fallback_reply: str = "Sorry, I could not complete that request."
    # Said when a newer message interrupts a turn AFTER something irreversible already happened:
    # the real operation is reported as done, never silently dropped (DESIGN 20.2).
    cancelled_after_effect_reply: str = (
        "Recebi sua nova mensagem. O que já estava em andamento foi concluído; "
        "vou cuidar do seu pedido agora."
    )
    flows: tuple[FlowDefinition, ...] = ()
    human_request: HumanRequest | None = None
    max_flow_depth: int = 3  # active + suspended flows kept per conversation
    max_tokens_per_turn: int | None = None  # guardrail: LLM tokens (in+out) per turn
    confirmation: ConfirmationTexts = ConfirmationTexts()
    confirmation_prompt_enabled: bool = True
    media: MediaTexts = MediaTexts()
    max_media_bytes: int = 25 * 1024 * 1024  # larger media is not fetched or transcribed
    # True: a reply with blank-line separated paragraphs goes out as one message per paragraph
    # (at most MAX_REPLY_PARTS), e.g. an answer and then the flow's question as its own message.
    split_replies: bool = False
    # Voice messages leave the system only when BOTH the operator configured a transcriber (it may
    # be a third-party API) AND the agent says `on`. The default is off: audio is personal data.
    transcription: Literal["off", "on"] = "off"

    @model_validator(mode="after")
    def _references_are_consistent(self) -> AgentDefinition:
        caps = [c.name for c in self.capabilities]
        tools = [t.name for t in self.tools]
        bound = [b.capability for b in self.bindings]
        for label, names in (("capability", caps), ("tool", tools), ("binding", bound)):
            if len(set(names)) != len(names):
                raise DefinitionError(f"duplicate {label} names in agent {self.agent_id!r}")
        for b in self.bindings:
            if b.capability not in caps:
                raise DefinitionError(f"binding references unknown capability {b.capability!r}")
            if b.tool not in tools:
                raise DefinitionError(f"binding references unknown tool {b.tool!r}")
        by_cap = {c.name: c for c in self.capabilities}
        by_tool = {t.name: t for t in self.tools}
        for b in self.bindings:
            floor = max(
                (by_cap[b.capability].risk, by_tool[b.tool].risk), key=lambda r: RISK_ORDER[r]
            )
            if b.risk is not None and RISK_ORDER[b.risk] < RISK_ORDER[floor]:
                raise DefinitionError(
                    f"binding {b.capability!r}->{b.tool!r} declares risk {b.risk!r}, lower than "
                    f"{floor!r}: a binding can raise protection but never lower it"
                )
        self._check_texts(by_cap)
        self._check_flows(by_cap)
        self._check_retry_against_effective_risk(by_cap, by_tool)
        self._check_recovery_lookups(by_cap, by_tool)
        self._check_safe_retry_recovery()
        for name in self.allowed_capabilities:
            if name not in bound:
                raise DefinitionError(f"allowed capability {name!r} has no binding")
        return self

    def _check_texts(self, by_cap: dict[str, CapabilityDefinition]) -> None:
        """Every runtime template must format, and a protected capability must always be asked
        about explicitly: the runtime-owned confirmation question is what a later "yes" answers."""
        c = self.confirmation
        summary = frozenset({"summary"})
        m = self.media
        check_template("media.audio", m.audio, {"text"}, required=frozenset({"text"}))
        check_template("media.document", m.document, {"name"})
        for label in (
            "audio_failed",
            "audio_disabled",
            "image",
            "video",
            "too_large",
            "unsupported",
            "unavailable",
        ):
            check_template(f"media.{label}", getattr(m, label), set())
        check_template("confirmation.prompt", c.prompt, {"summary"}, required=summary)
        check_template("confirmation.reprompt", c.reprompt, {"summary"}, required=summary)
        if c.reprompt_unproven is not None:
            check_template(
                "confirmation.reprompt_unproven", c.reprompt_unproven, {"summary"}, required=summary
            )
        check_template("confirmation.executed_fallback", c.executed_fallback, {"status"})
        for label in ("rejected", "expired", "gave_up"):
            check_template(f"confirmation.{label}", getattr(c, label), set())
        for cap in self.capabilities:
            if cap.summary_template:
                placeholders(cap.summary_template)  # well formed (field names: compiler)
        if not self.confirmation_prompt_enabled:
            protected = [
                n
                for n in sorted(self.allowed_capabilities)
                if (r := self.resolve(n)) is not None and r.requires_protection
            ]
            if protected:
                raise DefinitionError(
                    f"confirmation_prompt_enabled=false is not allowed while protected "
                    f"capabilities are allowed ({protected}): a pending action without an "
                    "explicit confirmation question could be confirmed by an unrelated yes"
                )

    def _check_flows(self, by_cap: dict[str, CapabilityDefinition]) -> None:
        """A flow only ever talks to Capabilities this agent has. Protection is decided by the
        RESOLVED binding (`requires_protection`), never by a label: reads in `Invoke`, protected
        ones in `Propose`, and digressions can use effective reads only."""
        if not self.flows:
            return
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise DefinitionError(f"unknown timezone {self.timezone!r}") from exc
        if self.max_flow_depth < 1:
            raise DefinitionError("max_flow_depth must be at least 1")
        names = [f.name for f in self.flows]
        if len(set(names)) != len(names):
            raise DefinitionError("duplicate flow names")
        seen: dict[str, str] = {}
        for flow in self.flows:
            for phrase in flow.triggers:
                owner = seen.setdefault(fold(phrase), flow.name)
                if owner != flow.name:
                    raise DefinitionError(
                        f"trigger {phrase!r} is declared by both {owner!r} and {flow.name!r}"
                    )
            for step in flow.steps:
                if isinstance(step, Invoke | Propose):
                    resolved = self.resolve(step.capability)
                    if resolved is None or step.capability not in self.allowed_capabilities:
                        raise DefinitionError(
                            f"flow {flow.name!r} step {step.id!r}: capability "
                            f"{step.capability!r} is not defined, bound and allowed"
                        )
                    if isinstance(step, Invoke) and resolved.requires_protection:
                        raise DefinitionError(
                            f"flow {flow.name!r} step {step.id!r}: invoke is for reads; use "
                            f"propose for the protected {step.capability!r} (effective risk "
                            f"{resolved.effective_risk!r})"
                        )
                    if isinstance(step, Propose) and not resolved.requires_protection:
                        raise DefinitionError(
                            f"flow {flow.name!r} step {step.id!r}: propose is for protected "
                            f"capabilities; {step.capability!r} is a plain read"
                        )
                if isinstance(step, Choose):
                    self._check_choose_fields(flow, step, by_cap)
            for name in flow.digression_capabilities:
                resolved = self.resolve(name)
                if resolved is None or resolved.requires_protection:
                    raise DefinitionError(
                        f"flow {flow.name!r}: a digression may only use effective read "
                        f"capabilities ({name!r} is not)"
                    )

    def _check_choose_fields(
        self, flow: FlowDefinition, step: Choose, by_cap: dict[str, CapabilityDefinition]
    ) -> None:
        """The list and item fields a Choose reads must exist in the source capability output."""
        source = next(s for s in flow.steps if isinstance(s, Invoke) and s.id == step.source)
        fields = by_cap[source.capability].output_model.model_fields
        field = fields.get(step.list_field)
        if field is None:
            raise DefinitionError(
                f"flow {flow.name!r} choose {step.id!r}: {source.capability!r} has no output "
                f"field {step.list_field!r}"
            )
        args = get_args(field.annotation)
        item = args[0] if get_origin(field.annotation) is list and args else None
        if isinstance(item, type) and issubclass(item, BaseModel):
            wanted = {step.value_field, *step.also.values()}
            if step.label:
                wanted |= set(placeholders(step.label))
            missing = sorted(wanted - set(item.model_fields))
            if missing:
                raise DefinitionError(
                    f"flow {flow.name!r} choose {step.id!r}: items of {step.list_field!r} have "
                    f"no field {missing[0]!r}"
                )

    def _check_retry_against_effective_risk(
        self, by_cap: dict[str, CapabilityDefinition], by_tool: dict[str, ToolDefinition]
    ) -> None:
        """A tool that looks like a read but is bound to a protected capability is a write for
        every runtime purpose, so the write-retry rules (INV-021) apply to it too."""
        for b in self.bindings:
            tool = by_tool[b.tool]
            risks = [by_cap[b.capability].risk, tool.risk] + ([b.risk] if b.risk else [])
            effective = max(risks, key=lambda r: RISK_ORDER[r])
            retry = tool.retry
            unsafe = not tool.idempotency_supported or retry.mode != "same_idempotency_key"
            if effective != "read" and retry.max_attempts > 1 and unsafe:
                raise DefinitionError(
                    f"tool {tool.name!r} is bound to {b.capability!r} (effective risk "
                    f"{effective!r}) and cannot retry without idempotency support and the "
                    "SAME idempotency key"
                )

    def _check_safe_retry_recovery(self) -> None:
        """`safe_retry` re-sends the frozen request: only an EFFECTIVE read may use it, however
        the tool labels itself (INV-021: an ambiguous write is never retried blind)."""
        for binding in self.bindings:
            resolved = self.resolve(binding.capability)
            assert resolved is not None
            recovery = resolved.tool.recovery
            if (
                recovery is not None
                and recovery.strategy == "safe_retry"
                and (resolved.requires_protection)
            ):
                raise DefinitionError(
                    f"tool {resolved.tool.name!r} declares recovery safe_retry but is bound to "
                    f"{binding.capability!r} with effective risk {resolved.effective_risk!r}"
                )

    def _check_recovery_lookups(
        self, by_cap: dict[str, CapabilityDefinition], by_tool: dict[str, ToolDefinition]
    ) -> None:
        """A status lookup must be a bound read that takes the idempotency key, and its result
        must be convertible into the result of the operation it recovers."""
        bound = {b.capability for b in self.bindings}
        for tool in self.tools:
            recovery = tool.recovery
            if recovery is None or recovery.strategy != "status_lookup":
                continue
            name = recovery.lookup_capability
            lookup = by_cap.get(name or "")
            if lookup is None or name not in bound:
                raise DefinitionError(
                    f"tool {tool.name!r}: lookup capability {name!r} is not defined and bound"
                )
            resolved_lookup = self.resolve(name or "")
            if resolved_lookup is None or resolved_lookup.requires_protection:
                raise DefinitionError(
                    f"tool {tool.name!r}: the status lookup must be an effective read "
                    "(no protected risk or confirmation on any layer)"
                )
            if "idempotency_key" not in lookup.input_model.model_fields:
                raise DefinitionError(
                    f"lookup {name!r} must take an `idempotency_key` (supplied by the runtime)"
                )
            for b in self.bindings:
                if b.tool != tool.name or recovery.result_map is not None:
                    continue
                original = by_cap[b.capability].output_model
                if schema_fingerprint(lookup.output_model) != schema_fingerprint(original):
                    raise DefinitionError(
                        f"lookup {name!r} returns a different schema than {b.capability!r}: "
                        "declare `recovery.result_map` to convert it"
                    )

    def resolve(self, capability_name: str) -> ResolvedToolBinding | None:
        binding = next((b for b in self.bindings if b.capability == capability_name), None)
        if binding is None:
            return None
        capability = next(c for c in self.capabilities if c.name == capability_name)
        tool = next(t for t in self.tools if t.name == binding.tool)
        return ResolvedToolBinding(capability=capability, tool=tool, binding=binding)
