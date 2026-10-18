# Contrato com o gateway de canal (RelayPlane)

> Fonte: o contrato **publicado** do RelayPlane (`github.com/Ppaulo03/RelayPlane`: `docs/CONTRACT.md`,
> `docs/EVENTS.md`, `docs/openapi.yaml`, lido no commit `880820a`). Este documento resume só o que os
> adaptadores do framework usam e o que a segurança da entrega assume. O simulador
> (`examples/reference-relayplane`) implementa este recorte; `tests/contracts/test_relayplane_live.py`
> (opt-in, `RELAYPLANE_*`) confere as premissas de segurança contra um gateway implantado.
> Divergência entre este texto e o repositório do RelayPlane: vale o repositório.

## Outbound (assíncrono)

`POST /api/v1/messages/send`, header `Idempotency-Key: <idempotency_key da linha da outbox>`, corpo
`{"instance_id", "to", "type": "text", "payload": {"text"}}` — idêntico em toda repetição técnica da linha.

| Resposta | Outbox | Observação |
|---|---|---|
| `202 {message_id, status: QUEUED}` | `QUEUED` + `channel_message_id` | aceite **durável** no gateway; ainda não do provedor |
| 429 | `FAILED` (retryable) | o gateway não tomou a mensagem |
| 400/404/409/413/422 | `FAILED` (não retryable) | recusada (422 = mesma chave, outro corpo: bug nosso) |
| 5xx, timeout, erro de rede, corpo ilegível, status desconhecido | `UNKNOWN` | pode ter sido aceita |

O resto chega depois, por evento `message.outbound_status` ou `GET /api/v1/messages/{id}`:
`QUEUED/DISPATCHING → QUEUED`; `ACCEPTED/DELIVERED/READ → ACCEPTED` (agora sim com `provider_message_id`
e `accepted_at`, relógio do **canal**, INV-026); `FAILED → FAILED`; `UNKNOWN → UNKNOWN`.
O estado só avança (`apply_channel_status`); evento atrasado nunca regride a linha.

**Não existe consulta por chave.** A primitiva segura de reconciliação é o próprio reenvio: a mesma
chave dentro da retenção devolve a mesma mensagem (`Idempotent-Replayed: true`).

### Retenção da idempotency key (premissa de segurança)

`GET /api/v1/limits → idempotency_retention_seconds` (padrão 24 h). Depois disso a chave é esquecida e
reenviar **cria uma mensagem nova**.

```text
retry_horizon <= idempotency_retention - safety_margin  (DeliveryPolicy genérica valida; RelayPlaneSender.delivery_policy() lê /limits)
```

A política e a semântica de entrega são genéricas (`RUNTIME_PROTOCOL.md` §8, ADR-023); o que segue é como o RelayPlane as instancia. O padrão de 24 h é **do gateway**: só este adapter o conhece, o código genérico recebe a janela da política.

- `RelayPlaneSender` satisfaz `MessageLookup` (`GET /messages/{id}`); `202 QUEUED` devolve o `message_id` como `channel_message_id` (o handle). Além do lookup, o gateway empurra status por webhook (`message.outbound_status`).
- Dentro de `retry_horizon`: o runtime reenvia a mesma linha com a mesma chave.
- Passado o horizon: um `FAILED` provado fica `FAILED`; um `SENDING` obsoleto vira `UNKNOWN` (sem chamada ao gateway).
- Reconciliador: com `channel_message_id` → `GET` e aplica o status (linha `QUEUED` antiga é consultada);
  sem id, com `retry_horizon > 0` e dentro de `retenção − margem` → volta a `PENDING` (reenvio = replay seguro);
  sem id e com `retry_horizon == 0` → `RESEND_NOT_PERMITTED`, continua `UNKNOWN`;
  sem id e fora da janela → `UNPROVEN_PAST_RETENTION`, continua `UNKNOWN` (alerta, nunca reenvio cego);
  erro no `GET` → `LOOKUP_FAILED`, nada é provado.
- `UNKNOWN` **do gateway** exige decisão humana (`POST /messages/{id}/resolve`): `RelayPlaneSender.resolve`
  existe para o operador, a automação nunca o chama (`CHANNEL_UNKNOWN_NEEDS_DECISION`).
- Esse `UNKNOWN` é uma **barreira de ordem da instância** no gateway (`docs/FAILURE-MODES.md` do RelayPlane):
  todos os envios seguintes da mesma instância, de todas as conversas daquele número, esperam até o
  `resolve` (ou até o `UNKNOWN_BARRIER_TIMEOUT`, se o operador configurou um). A POC reproduziu isso com um
  único 500 do provedor. Por isso o alerta `DeliveryUnknown` (runbook *unknown-delivery* em
  `OPERATIONS.md`) dispara em minutos, e não na janela de uma hora do `DeliveryUnsettled`.

## Inbound (webhook)

`POST {nosso_endpoint}/webhooks/relayplane/{subscription_id}` — at-least-once, sequência por
(subscription, instância) sem lacunas.

Headers: `X-RelayPlane-Timestamp`, `X-RelayPlane-Signature: v1=<hex>[,v1=<hex>...]`
(`HMAC-SHA256(secret, "<timestamp>.<corpo cru>")`; vários `v1` durante rotação), `X-RelayPlane-Event-Id`
(deve coincidir com o `event_id` do corpo). Timestamp fora de ±5 min é rejeitado.

Envelope: `schema_version` (`1` ou `2`; outro → 400), `event_id`, `sequence`, `event_type`, `tenant_id`,
`instance_id`, `timestamp`, `payload`. A versão 2 (R29 do RelayPlane, a que o gateway emite hoje) tirou
`provider` e `source_assignment` e acrescentou `channel` (`whatsapp`); nenhum deles é lido aqui, então as
duas versões têm o mesmo efeito. A 1 continua aceita por causa dos gateways até `0.3.0-rc3`.

| `event_type` | Efeito |
|---|---|
| `message.received` | `InboundEvent` no inbox (dedupe por `event_id`); conversa = `{instance_id}:{from}` |
| `message.outbound_status` | status do que enviamos → outbox (só avança) |
| `message.deleted` | retira do inbox o evento ainda não consumido por um turno (`READY → DEAD`) |
| `message.status`, `instance.status_changed`, outros | reconhecidos (200) e ignorados |

Fora de escopo, reconhecidos e ignorados: mensagens de grupo (`chat_id`) e edições `secretEncrypted`.
Suporte futuro a grupos (campos que o gateway precisaria entregar): [`FUTURE_GROUPS.md`](./FUTURE_GROUPS.md), seção 4.
Mídia chega como referência com status do gateway (`READY` / `REJECTED` / `FAILED`); bytes só por
`GET /api/v1/media/{id}/content`, com `ETag` = sha256 verificado (`RelayPlaneMediaFetcher`).

Respostas: `200` aceito/duplicado/ignorado; `400` envelope/payload inválido; `401` assinatura;
`404` subscription; `409` identidade de conversa conflitante; `413` corpo grande; `503` não persistiu
(o gateway reentrega). **Nada é reconhecido antes de persistido.** Nosso tenant vem do cadastro da
subscription (`Subscription.tenant_id`), nunca do corpo; `relay_tenant_id`/`instance_ids` opcionais filtram.

## O que o gateway NÃO oferece (e portanto não é prometido aqui)

Templates / janela de 24 h (a política de serviço é nossa: `ChannelPolicy`), consulta por chave de
idempotência, grupos.

## Variáveis do teste contra o gateway real

`RELAYPLANE_BASE_URL`, `RELAYPLANE_TOKEN`, `RELAYPLANE_CHANNEL_ID` (= `instance_id`), `RELAYPLANE_TO`,
`RELAYPLANE_RETRY_HORIZON_SECONDS` e (opcional, demorado) `RELAYPLANE_MEASURE_RETENTION=1`, que espera a
retenção reportada por `/limits` e confirma que a chave ainda é lembrada.
