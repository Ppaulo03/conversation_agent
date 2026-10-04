# Contrato assumido com o gateway de canal ("RelayPlane")

> **Isto NÃO é a especificação do fornecedor.** É o contrato que este projeto assume, escrito para
> ser verificado. O simulador (`examples/reference-relayplane`) implementa exatamente isto, e
> `tests/contracts/test_relayplane_live.py` roda as mesmas verificações contra um gateway real
> (variáveis `RELAYPLANE_*`, ver abaixo). Qualquer divergência encontrada no gateway real é um
> bug deste documento, não do gateway.

## Outbound

`POST {base_url}/v1/messages`, header `Idempotency-Key: <outbox idempotency_key>`, corpo
`{"channel_id", "to", "text"}` — idêntico em toda repetição técnica da mesma linha da outbox.

| Resposta | Outbox | Observação |
|---|---|---|
| 200/202 `{id, status: "accepted", accepted_at}` | `ACCEPTED` | `accepted_at` é o relógio do **canal** (INV-026); sem ele, `NULL` |
| 200/202 `{status: "queued"}` | `QUEUED` | não autoriza confirmação |
| 408, 429 | `FAILED` (retryable) | o gateway não tomou a mensagem |
| outros 4xx | `FAILED` (não retryable) | recusada |
| 409 | `UNKNOWN` | chave em uso/reusada com outro corpo: nunca assumir |
| 5xx, timeout, erro de rede, corpo ilegível, status desconhecido | `UNKNOWN` | pode ter sido aceita |

`GET {base_url}/v1/messages?idempotency_key=K` → `200 {id, status, accepted_at}` ou `404`.
`404` **só prova ausência dentro da janela de retenção** (abaixo).

### Retenção da idempotency key (premissa de segurança)

O gateway deduplica por `Idempotency-Key` por `relayplane_idempotency_retention`. Depois disso a chave
é esquecida: reenviar duplicaria, e um `404` no lookup deixa de provar ausência.

```text
sender_retry_horizon <= relayplane_idempotency_retention          (DeliveryPolicy valida)
```

- Dentro de `retry_horizon`: o sender reenvia a mesma linha com a mesma chave.
- Passado o horizon: o sender **não** reenvia; a linha vira `UNKNOWN` e o reconciliador pergunta ao gateway.
- Reconciliação: encontrada → registra; `404` com idade ≤ `retention − margem` → reenvio com a mesma chave;
  `404` mais velho, ou erro no lookup → continua `UNKNOWN` (alerta); nunca reenvio cego.

## Inbound (webhook)

`POST {nosso_endpoint}/webhooks/relayplane/{subscription_id}` — at-least-once.

Assinatura: header `X-Relay-Signature: t=<unix>,v1=<hex>[,v1=<hex>...]`, `v1 = HMAC-SHA256(secret, "<t>.<corpo cru>")`.
Vários `v1` permitem rotação de segredo. `t` fora de ±5 min é rejeitado (replay).

Corpo (`message.received`): `id` (dedupe), `channel_id`, `conversation_id`, `contact_id`, `session_id?`,
`occurred_at` (relógio do canal), `sequence?`, `reply_to?`, `text?`, `media[]` (referências:
`media_id, kind, mime_type, size_bytes?, url?, sha256?, filename?`). Outros `type` são reconhecidos (200) e ignorados.

Respostas: `200` aceito/duplicado/ignorado; `400` payload inválido; `401` assinatura; `404` subscription;
`409` identidade de conversa conflitante; `413` corpo grande; `503` não persistiu (o gateway reentrega).
**Nada é reconhecido antes de persistido.** Tenant e canal vêm do cadastro da subscription, nunca do corpo.

## Variáveis do teste contra o gateway real

`RELAYPLANE_BASE_URL`, `RELAYPLANE_TOKEN`, `RELAYPLANE_CHANNEL_ID`, `RELAYPLANE_TO`,
`RELAYPLANE_IDEMPOTENCY_RETENTION_SECONDS`, `RELAYPLANE_RETRY_HORIZON_SECONDS` e (opcional, demorado)
`RELAYPLANE_MEASURE_RETENTION=1`, que espera a janela e mede se a chave realmente foi esquecida.