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
sender_retry_horizon <= idempotency_retention          (DeliveryPolicy valida; delivery_policy() lê /limits)
```

- Dentro de `retry_horizon`: o sender reenvia a mesma linha com a mesma chave.
- Passado o horizon: o sender **não** envia; a linha vira `UNKNOWN` (sem chamada ao gateway).
- Reconciliador: com `channel_message_id` → `GET` e aplica o status (linha `QUEUED` antiga é consultada);
  sem id e dentro de `retenção − margem` → volta a `PENDING` (reenvio = replay seguro);
  sem id e fora da janela → `UNPROVEN_PAST_RETENTION`, continua `UNKNOWN` (alerta, nunca reenvio cego);
  erro no `GET` → `LOOKUP_FAILED`, nada é provado.
- `UNKNOWN` **do gateway** exige decisão humana (`POST /messages/{id}/resolve`): `RelayPlaneSender.resolve`
  existe para o operador, a automação nunca o chama (`CHANNEL_UNKNOWN_NEEDS_DECISION`).

## Inbound (webhook)

`POST {nosso_endpoint}/webhooks/relayplane/{subscription_id}` — at-least-once, sequência por
(subscription, instância) sem lacunas.

Headers: `X-RelayPlane-Timestamp`, `X-RelayPlane-Signature: v1=<hex>[,v1=<hex>...]`
(`HMAC-SHA256(secret, "<timestamp>.<corpo cru>")`; vários `v1` durante rotação), `X-RelayPlane-Event-Id`
(deve coincidir com o `event_id` do corpo). Timestamp fora de ±5 min é rejeitado.

Envelope: `schema_version` (só `1`; outro → 400), `event_id`, `sequence`, `event_type`, `tenant_id`,
`instance_id`, `timestamp`, `payload`.

| `event_type` | Efeito |
|---|---|
| `message.received` | `InboundEvent` no inbox (dedupe por `event_id`); conversa = `{instance_id}:{from}` |
| `message.outbound_status` | status do que enviamos → outbox (só avança) |
| `message.deleted` | retira do inbox o evento ainda não consumido por um turno (`READY → DEAD`) |
| `message.status`, `instance.status_changed`, outros | reconhecidos (200) e ignorados |

Fora de escopo, reconhecidos e ignorados: mensagens de grupo (`chat_id`) e edições `secretEncrypted`.
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
