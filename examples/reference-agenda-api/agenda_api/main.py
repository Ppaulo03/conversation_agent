"""Reference agenda API: a SECOND external scheduling system, deliberately unlike the first.

It exists to prove a Pack is reusable: the same scheduling behavior must work over this API using
only different bindings and mappings. It knows nothing about conversation_agent.

How it differs from `scheduling_api` (so the bindings have real work to do):

  vocabulary      servico/de/ate, horarios/inicio/fim, codigo/situacao (not service_code/items/...)
  paths           /v2/agenda/horarios, /v2/agenda/reservas, .../reservas/por-chave/{chave}
  time            answers in UTC ("Z"), takes a UTC `inicio`; the business runs in America/Manaus
  duration        `duracao_min` in MINUTES (the first API takes hours)
  pagination      page tokens ("p2"), not offsets
  service codes   MSG (45 min) and PIL (50 min); a 30-minute grid, Monday to Saturday
  errors          a taken slot is 422 HORARIO_INDISPONIVEL (the first answers 409); the status
                  words are CONFIRMADA / CANCELADA
  idempotency     the same `Idempotency-Key` header: replay is 200 with the same reservation
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

TZ = ZoneInfo("America/Manaus")
SERVICOS: dict[str, int] = {"MSG": 45, "PIL": 50}  # codigo -> minutes
JANELAS = ((time(8, 0), time(12, 0)), (time(13, 0), time(19, 0)))
GRADE = timedelta(minutes=30)
MAX_DIAS = 14
POR_PAGINA = 10


def _erro(status: int, codigo: str, mensagem: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"codigo": codigo, "mensagem": mensagem})


def _utc(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def create_app(
    *,
    now: Callable[[], datetime] | None = None,
    taken_slots: set[datetime] | None = None,
) -> FastAPI:
    clock = now or (lambda: datetime.now(TZ))
    app = FastAPI(title="reference-agenda-api")
    app.state.requests = []  # observed requests (test visibility only)
    app.state.fault = None  # test-only fault injection
    app.state.taken_slots = taken_slots or set()
    app.state.reservas = {}  # codigo -> reserva (the business state lives HERE)
    app.state.by_key = {}  # idempotency key -> (fingerprint, codigo)

    @app.middleware("http")
    async def observe_and_inject_faults(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        app.state.requests.append(
            {
                "method": request.method,
                "path": request.url.path,
                "query": dict(request.query_params),
                "headers": {k.lower(): v for k, v in request.headers.items()},
            }
        )
        fault: dict[str, Any] | None = app.state.fault
        if fault and request.url.path != "/health":
            if "delay" in fault:
                await asyncio.sleep(fault["delay"])
            if "status" in fault:  # fails before any effect
                return _erro(fault["status"], "FALHA_INJETADA", "injected fault")
            response = await call_next(request)  # the effect HAPPENS...
            if "status_after_effect" in fault:  # ...but the caller only sees an error
                return _erro(fault["status_after_effect"], "FALHA_INJETADA", "response lost")
            return response
        return await call_next(request)

    def horarios_livres(servico: str, de: date, ate: date) -> list[tuple[datetime, datetime]]:
        duracao = timedelta(minutes=SERVICOS[servico])
        agora = clock()
        ocupados = [
            (r["inicio"], r["fim"])
            for r in app.state.reservas.values()
            if r["situacao"] == "CONFIRMADA"
        ]
        livres: list[tuple[datetime, datetime]] = []
        dia = de
        while dia <= ate:
            if dia.weekday() < 6:  # Monday to Saturday
                for abre, fecha in JANELAS:
                    inicio = datetime.combine(dia, abre, tzinfo=TZ)
                    fim_janela = datetime.combine(dia, fecha, tzinfo=TZ)
                    while inicio + duracao <= fim_janela:
                        fim = inicio + duracao
                        ocupado = inicio in app.state.taken_slots or any(
                            inicio < o_fim and fim > o_inicio for o_inicio, o_fim in ocupados
                        )
                        if inicio > agora and not ocupado:
                            livres.append((inicio, fim))
                        inicio += GRADE
            dia += timedelta(days=1)
        return livres

    def publica(reserva: dict[str, Any]) -> dict[str, Any]:
        return {
            "codigo": reserva["codigo"],
            "servico": reserva["servico"],
            "inicio": _utc(reserva["inicio"]),
            "situacao": reserva["situacao"],
        }

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v2/agenda/horarios")
    async def horarios(servico: str, de: date, ate: date, pagina: str | None = None) -> Response:
        if servico not in SERVICOS:
            return _erro(404, "SERVICO_INEXISTENTE", f"servico {servico} desconhecido")
        if ate < de or (ate - de).days >= MAX_DIAS:
            return _erro(400, "PERIODO_INVALIDO", "ate deve ser >= de e caber em 14 dias")
        try:
            numero = int(pagina[1:]) if pagina else 1
            if pagina and not pagina.startswith("p"):
                raise ValueError
        except ValueError:
            return _erro(400, "PAGINA_INVALIDA", "token de pagina invalido")
        livres = horarios_livres(servico, de, ate)
        corte = (numero - 1) * POR_PAGINA
        pedaco = livres[corte : corte + POR_PAGINA]
        proxima = f"p{numero + 1}" if corte + POR_PAGINA < len(livres) else None
        return JSONResponse(
            {
                "horarios": [{"inicio": _utc(i), "fim": _utc(f)} for i, f in pedaco],
                "proxima_pagina": proxima,
            }
        )

    @app.post("/v2/agenda/reservas")
    async def reservar(request: Request) -> Response:
        chave = request.headers.get("idempotency-key")
        if not chave:
            return _erro(400, "CHAVE_OBRIGATORIA", "Idempotency-Key e obrigatoria")
        try:
            corpo = await request.json()
            servico = str(corpo["servico"])
            inicio = datetime.fromisoformat(str(corpo["inicio"]).replace("Z", "+00:00")).astimezone(
                TZ
            )
            duracao_min = int(corpo["duracao_min"])
        except (ValueError, KeyError, TypeError):
            return _erro(400, "CORPO_INVALIDO", "servico, inicio e duracao_min sao obrigatorios")

        impressao = hashlib.sha256(
            json.dumps([servico, inicio.isoformat(), duracao_min]).encode()
        ).hexdigest()
        if chave in app.state.by_key:  # idempotent replay
            guardada, codigo = app.state.by_key[chave]
            if guardada != impressao:
                return _erro(422, "CHAVE_REUTILIZADA", "chave usada com outro pedido")
            return JSONResponse(publica(app.state.reservas[codigo]), status_code=200)

        if servico not in SERVICOS or duracao_min != SERVICOS[servico]:
            return _erro(
                400, "SERVICO_OU_DURACAO_INVALIDOS", "servico desconhecido ou duracao errada"
            )
        dia = inicio.date()
        if inicio not in [i for i, _ in horarios_livres(servico, dia, dia)]:
            return _erro(
                422, "HORARIO_INDISPONIVEL", "o horario nao esta livre"
            )  # no double booking

        codigo = f"R-{len(app.state.reservas) + 1}"
        reserva = {
            "codigo": codigo,
            "servico": servico,
            "inicio": inicio,
            "fim": inicio + timedelta(minutes=duracao_min),
            "situacao": "CONFIRMADA",
        }
        app.state.reservas[codigo] = reserva
        app.state.by_key[chave] = (impressao, codigo)
        return JSONResponse(publica(reserva), status_code=201)

    @app.get("/v2/agenda/reservas/por-chave/{chave}")
    async def reserva_por_chave(chave: str) -> Response:
        if chave not in app.state.by_key:
            return _erro(404, "RESERVA_NAO_ENCONTRADA", "nenhuma reserva para esta chave")
        return JSONResponse(publica(app.state.reservas[app.state.by_key[chave][1]]))

    return app


app = create_app()
