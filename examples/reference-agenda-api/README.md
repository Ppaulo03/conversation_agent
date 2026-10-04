# reference-agenda-api

A **second external scheduling system**, deliberately unlike `reference-scheduling-api`: other
vocabulary (`servico/de/ate`, `horarios/inicio/fim`, `codigo/situacao`), other paths
(`/v2/agenda/...`), UTC answers, `duracao_min` in minutes (the first API takes hours), page tokens
instead of offsets, `422 HORARIO_INDISPONIVEL` for a taken slot, services `MSG` (45 min) and `PIL`
(50 min) on a 30-minute grid, Monday to Saturday in America/Manaus.

It exists to prove that the `generic-scheduling` Pack is reusable: the same behavior runs over this
API with only different bindings and mappings. `conversation_agent` never imports it (import-linter).

| Method | Path | Notes |
|---|---|---|
| GET | `/v2/agenda/horarios?servico=MSG&de=...&ate=...&pagina=p2` | free slots, 10 per page |
| POST | `/v2/agenda/reservas` | body `{servico, inicio (UTC), duracao_min}`, header `Idempotency-Key` required |
| GET | `/v2/agenda/reservas/por-chave/{chave}` | the reservation made with that idempotency key |
