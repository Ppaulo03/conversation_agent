"""The scheduling agent, assembled from typed Python objects (no Pack, no YAML).

Everything domain-specific lives here: the engine never learns what "scheduling" means.

  Capability (stable semantic contract)   ->  scheduling.availability / scheduling.create
  Tool       (concrete ERP-style operation) ->  erp_get_available_slots / erp_create_reservation
  Binding    (adapts one to the other)      ->  input/output/error mappings
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from conversation_agent.core.definitions.agent import AgentDefinition, ConfirmationTexts
from conversation_agent.core.definitions.binding import CapabilityBinding, ErrorMap, ErrorRule
from conversation_agent.core.definitions.capability import CapabilityDefinition
from conversation_agent.core.definitions.flow import (
    AddDays,
    Ask,
    Choose,
    Collect,
    FlowDefinition,
    Invoke,
    Propose,
    Say,
    SlotDefinition,
    Table,
)
from conversation_agent.core.definitions.flow import Slot as FlowSlot
from conversation_agent.core.definitions.mapping import Const, Each, Ref
from conversation_agent.core.definitions.tool import HTTPRequestSpec, RecoverySpec, ToolDefinition

CONNECTION = "scheduling_api"
ServiceId = Literal["haircut", "consultation"]
_SERVICE_TO_CODE = {"haircut": "HC-01", "consultation": "CN-01"}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- Capability contracts (what the LLM / Flow sees) -----------------------------------


class AvailabilityInput(_Strict):
    service_id: ServiceId = Field(description="haircut (30 min) or consultation (60 min)")
    from_date: date = Field(description="First day to search, YYYY-MM-DD")
    to_date: date = Field(description="Last day to search (inclusive), YYYY-MM-DD, max 14 days")
    cursor: str | None = Field(default=None, description="Pagination cursor from a previous call")


class Slot(_Strict):
    start_at: AwareDatetime
    end_at: AwareDatetime


class AvailabilityOutput(_Strict):
    slots: list[Slot]
    next_cursor: str | None


class CreateInput(_Strict):
    service_id: ServiceId
    start_at: AwareDatetime = Field(description="Slot start, ISO-8601 WITH timezone offset")
    duration_minutes: int = Field(gt=0, description="Duration of the service in minutes")


class CreateOutput(_Strict):
    booking_id: str
    status: str


class LookupInput(_Strict):
    idempotency_key: str  # supplied by the runtime during reconciliation, never by the LLM


LOOKUP = CapabilityDefinition(
    name="scheduling.lookup_booking",
    description="Finds the booking created with a given idempotency key (runtime/recovery use).",
    input_model=LookupInput,
    output_model=CreateOutput,
    risk="read",
)

AVAILABILITY = CapabilityDefinition(
    name="scheduling.availability",
    description=(
        "Lists the free appointment slots for a service in a date range. "
        "This is the ONLY source of availability; never invent slots."
    ),
    input_model=AvailabilityInput,
    output_model=AvailabilityOutput,
    risk="read",
)

CREATE = CapabilityDefinition(
    name="scheduling.create",
    description="Books an appointment for the current contact in a slot returned by availability.",
    input_model=CreateInput,
    output_model=CreateOutput,
    risk="irreversible",
    confirmation_required=True,
    summary_template="Agendar {service_id} em {start_at} ({duration_minutes} min)",
)


# --- Tools (concrete operations of the external ERP-like API) ---------------------------


class ErpAvailabilityArgs(_Strict):
    service_code: str
    start: date
    end: date
    cursor: str | None = None
    limit: int = 20


class ErpItem(BaseModel):
    start: str
    end: str


class ErpPagination(BaseModel):
    next_cursor: str | None = None


class ErpAvailabilityResponse(BaseModel):
    items: list[ErpItem]
    pagination: ErpPagination


class ErpCreateArgs(_Strict):
    service_code: str
    starts_at: str
    hours: float


class ErpCreateResponse(BaseModel):
    id: str
    state: str


ERP_GET_AVAILABLE_SLOTS = ToolDefinition(
    name="erp_get_available_slots",
    description="GET /availability on the scheduling API.",
    input_model=ErpAvailabilityArgs,
    output_model=ErpAvailabilityResponse,
    risk="read",
    timeout_seconds=5.0,
    provider="http",
    connection=CONNECTION,
    http=HTTPRequestSpec(
        method="GET",
        path="/availability",
        query=("service_code", "start", "end", "cursor", "limit"),
    ),
)

ERP_CREATE_RESERVATION = ToolDefinition(
    name="erp_create_reservation",
    description="POST /bookings on the scheduling API (idempotent by Idempotency-Key).",
    input_model=ErpCreateArgs,
    output_model=ErpCreateResponse,
    risk="irreversible",
    confirmation_required=True,
    timeout_seconds=5.0,
    provider="http",
    connection=CONNECTION,
    http=HTTPRequestSpec(
        method="POST", path="/bookings", body=("service_code", "starts_at", "hours")
    ),
    idempotency_supported=True,
    # An unknown outcome is resolved by asking the ERP what it did (never by blind retry).
    recovery=RecoverySpec(
        strategy="status_lookup",
        lookup_capability="scheduling.lookup_booking",
        # ONLY this answer proves the booking was never created (any other error proves nothing).
        absent_codes=("BOOKING_NOT_FOUND",),
    ),
)


class ErpFindArgs(_Strict):
    key: str


ERP_FIND_RESERVATION = ToolDefinition(
    name="erp_find_reservation",
    description="GET /bookings/by-idempotency-key/{key} on the scheduling API.",
    input_model=ErpFindArgs,
    output_model=ErpCreateResponse,
    risk="read",
    provider="http",
    connection=CONNECTION,
    http=HTTPRequestSpec(method="GET", path="/bookings/by-idempotency-key/{key}"),
)


# --- Bindings (Capability <-> Tool adaptation) ------------------------------------------

_SERVICE_CODE = Ref(from_="$.service_id", enum=_SERVICE_TO_CODE)

AVAILABILITY_BINDING = CapabilityBinding(
    capability="scheduling.availability",
    tool="erp_get_available_slots",
    input_map={
        "service_code": _SERVICE_CODE,
        "start": "$.from_date",
        "end": "$.to_date",
        "cursor": "$.cursor",
        "limit": Const(const=20),
    },
    output_map={
        "slots": Each(from_each="$.items[*]", map={"start_at": "$.start", "end_at": "$.end"}),
        "next_cursor": "$.pagination.next_cursor",
    },
    error_map=ErrorMap(
        http_status={
            404: ErrorRule(type="business_error", code="SERVICE_NOT_FOUND"),
            422: ErrorRule(type="validation_error"),
        },
        default_5xx={"read": ErrorRule(type="technical_error", retryable=True)},
        timeout={"read": ErrorRule(type="timeout", retryable=True)},
    ),
)

CREATE_BINDING = CapabilityBinding(
    capability="scheduling.create",
    tool="erp_create_reservation",
    input_map={
        "service_code": _SERVICE_CODE,
        "starts_at": Ref(from_="$.start_at", transform="datetime_to_utc"),
        "hours": Ref(from_="$.duration_minutes", transform="minutes_to_hours"),
    },
    output_map={"booking_id": "$.id", "status": "$.state"},
    error_map=ErrorMap(
        http_status={409: ErrorRule(type="business_error", code="SLOT_UNAVAILABLE")},
        default_5xx={"write": ErrorRule(type="unknown")},
        timeout={"write": ErrorRule(type="unknown")},
    ),
)

LOOKUP_BINDING = CapabilityBinding(
    capability="scheduling.lookup_booking",
    tool="erp_find_reservation",
    input_map={"key": "$.idempotency_key"},
    output_map={"booking_id": "$.id", "status": "$.state"},
    error_map=ErrorMap(
        http_status={404: ErrorRule(type="business_error", code="BOOKING_NOT_FOUND")},
        default_5xx={"read": ErrorRule(type="technical_error", retryable=True)},
        timeout={"read": ErrorRule(type="timeout", retryable=True)},
    ),
)

PERSONA = """\
Você é a assistente virtual de agendamentos de uma clínica/salão. Responda sempre em \
português do Brasil, de forma curta, cordial e objetiva.

Serviços disponíveis: "haircut" (corte de cabelo, 30 min) e "consultation" (consulta, 60 min).

Como trabalhar:
1. Descubra qual serviço e qual dia/período o cliente quer (peça só o que faltar).
2. Consulte horários SOMENTE com a ferramenta de disponibilidade e ofereça no máximo 5 opções \
reais, mostrando dia e hora no fuso America/Sao_Paulo.
3. Se não houver horário no dia pedido, diga isso e busque/ofereça alternativas em outros dias \
com a ferramenta; nunca invente horários.
4. Se o cliente corrigir dia, horário ou serviço, atualize o que foi combinado e, se preciso, \
consulte de novo.
5. Quando o cliente escolher um horário que veio da ferramenta, registre a proposta de \
agendamento (ferramenta de criação). Ela apenas REGISTRA uma proposta: nada é agendado ainda. \
O SISTEMA fará a pergunta de confirmação ao cliente logo após a sua mensagem; portanto resuma \
a proposta em uma frase, diga que ainda não foi efetivada e NÃO peça confirmação por conta própria.
6. Se uma ferramenta falhar, avise que não conseguiu consultar agora e peça para tentar de \
novo mais tarde; nunca finja que deu certo.
"""


# --- The scheduling Flow: conversation only (collect, search, choose, propose) ----------------

_RETRY = (
    "Não consegui consultar a agenda agora. Me escreva de novo em instantes que eu tento outra vez."
)

SCHEDULING_FLOW = FlowDefinition(
    name="scheduling",
    description="Schedule a haircut or a consultation.",
    triggers=(
        "agendar",
        "agendamento",
        "marcar",
        "marcar horario",
        "cortar cabelo",
        "corte de cabelo",
        "consulta",
    ),
    slots=(
        SlotDefinition(
            name="service",
            type="enum",
            prompt="Qual serviço você quer: corte de cabelo (30 min) ou consulta (60 min)?",
            choices={
                "haircut": ("corte", "cortar", "cabelo", "corte de cabelo"),
                "consultation": ("consulta", "consultar"),
            },
            invalidates=("start_at",),
        ),
        SlotDefinition(
            name="date",
            type="date",
            prompt="Para qual dia você quer agendar?",
            invalidates=("start_at",),
        ),
        # stated by the user, never asked: narrows the real options when it matches exactly one
        SlotDefinition(
            name="preferred_time", type="time", prompt="", required=False, invalidates=("start_at",)
        ),
        SlotDefinition(name="start_at", type="text", prompt="", required=False),
    ),
    steps=(
        Collect(id="need", slots=("service", "date")),
        Invoke(
            id="search",
            capability="scheduling.availability",
            inputs={
                "service_id": FlowSlot(name="service"),
                "from_date": FlowSlot(name="date"),
                "to_date": AddDays(base=FlowSlot(name="date"), days=0),
            },
            on={
                "validation_error": Ask(slot="date", text="Não consegui usar essa data."),
                "business_error": Say(text="Esse serviço não está disponível agora.", end=True),
                "policy_denied": Say(
                    text="Não posso consultar a agenda neste atendimento.", end=True
                ),
            },
            default=Say(text=_RETRY),  # technical_error / timeout / unknown: stay, retry on reply
        ),
        Choose(
            id="pick",
            source="search",
            list_field="slots",
            value_field="start_at",
            into="start_at",
            prompt="Tenho estes horários para {date}:\n{options}\nQual você prefere?",
            empty=Ask(slot="date", text="Não tenho horários livres em {date}."),
            prefer_time_slot="preferred_time",
            no_match_text="Não tenho exatamente esse horário.",
        ),
        Propose(
            id="book",
            capability="scheduling.create",
            inputs={
                "service_id": FlowSlot(name="service"),
                "start_at": FlowSlot(name="start_at"),
                "duration_minutes": Table(
                    key=FlowSlot(name="service"), mapping={"haircut": 30, "consultation": 60}
                ),
            },
            intro="Perfeito, vou registrar esse horário.",
            on={
                "business_error": Ask(
                    slot="date", text="Esse horário acabou de ser ocupado. Vamos escolher outro."
                ),
                "policy_denied": Say(text="Não posso agendar neste atendimento.", end=True),
            },
            default=Say(text="Não consegui concluir o agendamento agora.", end=True),
        ),
    ),
    cancelled_reply="Tudo bem, não vou agendar nada.",
    digression_capabilities=("scheduling.availability",),
)


def build_agent(*, flows: bool = False, transcription: str = "off") -> AgentDefinition:
    return AgentDefinition(
        agent_id="scheduling-demo",
        version="0.1.0",
        persona=PERSONA,
        timezone="America/Sao_Paulo",
        capabilities=(AVAILABILITY, CREATE, LOOKUP),
        tools=(ERP_GET_AVAILABLE_SLOTS, ERP_CREATE_RESERVATION, ERP_FIND_RESERVATION),
        bindings=(AVAILABILITY_BINDING, CREATE_BINDING, LOOKUP_BINDING),
        allowed_capabilities=frozenset({"scheduling.availability", "scheduling.create"}),
        flows=(SCHEDULING_FLOW,) if flows else (),
        transcription=transcription,
        fallback_reply="Desculpe, não consegui concluir agora. Pode tentar novamente?",
        confirmation=ConfirmationTexts(
            prompt="Posso confirmar? {summary}. Responda SIM para confirmar ou NÃO para cancelar.",
            reprompt="Não entendi. {summary}. Responda SIM para confirmar ou NÃO para cancelar.",
            reprompt_unproven=(
                'Não consegui confirmar que o seu "sim" responde à minha pergunta. {summary}. '
                "Responda SIM ou NÃO respondendo diretamente à mensagem acima."
            ),
            rejected="Tudo bem, cancelei o pedido.",
            expired="Esse pedido expirou. Me diga de novo o que você gostaria de fazer.",
            gave_up="Não consegui uma confirmação clara, então cancelei o pedido.",
            executed_fallback="Seu pedido foi processado (situação: {status}).",
        ),
    )
