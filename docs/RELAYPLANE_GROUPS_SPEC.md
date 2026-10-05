# RelayPlane: o que o gateway precisa entregar para o bot atender grupos

> **Status: FUTURO. Nada disto está implementado.** É o pedido do `conversation_agent` ao RelayPlane.
> Hoje o bot ignora toda mensagem com `chat_id` (grupo) e responde `200 ignored`. Para atender grupos
> com segurança ele precisa de fatos estruturados que só o gateway sabe. Este documento define esses
> fatos; a POLÍTICA (quem pode falar, quando responder) fica no bot (`docs/FUTURE_GROUPS.md`).

## 1. Princípio

O gateway **informa fatos do canal**; o bot **decide o que fazer com eles**. O gateway nunca decide
"responder ou não". O bot nunca deduz fatos do canal lendo o texto da mensagem (ex.: procurar
"@nome" no texto), porque isso quebra quando o provedor muda a forma de marcar alguém.

## 2. `message.received`: campos novos (todos aditivos)

Hoje o payload tem `from`, `type`, `chat_id` (só em grupo), `text`, `media`, `provider_message_id`,
`reply_to_provider_message_id`, `timestamp`. Pedido:

| Campo | Tipo | Regra |
|---|---|---|
| `chat_type` | `"direct"` \| `"group"` \| `"other"` | SEMPRE presente. `other` = broadcast, canal, comunidade ou o que o gateway não souber classificar. |
| `chat_id` | string | Mantido: presente **somente** quando `chat_type != "direct"` (compatibilidade). |
| `from` | string | **Autor da mensagem** em qualquer tipo de chat (em grupo, o participante, nunca o id do grupo). Normalizado do mesmo jeito que hoje (E.164 sem `+`, ou o id estável do provedor quando não houver número). |
| `mentioned` | boolean | `true` se esta mensagem menciona (@) a **própria instância**, em qualquer formato que o provedor use (número, LID, JID). Em chat direto: `false`. SEMPRE presente. |
| `mentions` | string[] | Ids normalizados de TODOS os mencionados (pode estar vazio). Opcional. |
| `quoted_from_me` | boolean | `true` se a mensagem citada (`reply_to_provider_message_id`) foi enviada por esta instância. Responder a uma mensagem do bot conta como falar com o bot. SEMPRE presente (`false` sem citação). |
| `group` | objeto, só em grupo | `{ "subject": string \| null, "participants": integer \| null }`. Opcional e **desligável**: o nome do grupo é dado pessoal. |

Notas:

- `from` em grupo é o campo mais crítico: o bot precisa saber QUEM falou para impedir que o "sim" de um
  participante confirme a ação de outro.
- O gateway deve resolver a identidade da própria instância (número, LID e JID) uma vez e usá-la para
  `mentioned` e `quoted_from_me`. O bot não conhece esses formatos.
- Mensagens de sistema do grupo (entrou, saiu, mudou o assunto, mudou foto) **não** são
  `message.received`. Se o gateway quiser expô-las, que seja outro `event_type` (ex.: `group.updated`);
  o bot as ignora.
- Edições e remoções (`message.deleted`) em grupo devem trazer `chat_id` e o `from` do autor.

## 3. Assinatura do webhook: entrega de grupos por opt-in

Para não entupir bots que não querem grupos e manter o comportamento atual:

- Cada subscription ganha `include_groups: boolean` (padrão `false` = comportamento de hoje: grupos
  reconhecidos e descartados no gateway, sem entregar).
- Com `true`, `message.received` de grupo é entregue com os campos da seção 2.
- Opcional e recomendado: `group_ids: string[]` na subscription (vazio = todos) para limitar quais grupos
  o gateway entrega. É um filtro de VOLUME; a política de quem o bot atende continua no bot.

## 4. Envio para grupo (`POST /messages`)

O envio já é idempotente por `Idempotency-Key`; isto não muda. Pedido de campos novos, opcionais:

| Campo | Regra |
|---|---|
| `to` | Aceita o id do grupo (o mesmo `chat_id` recebido). |
| `reply_to_provider_message_id` | Envia citando a mensagem (já existe para chat direto: garantir em grupo). |
| `mentions` | string[] de ids (os mesmos formatos de `from`) a marcar no texto; o gateway monta a marcação do provedor. O texto contém o placeholder acordado (ex.: `@{from}`) ou o gateway anexa a marcação, o que preferir, desde que documentado. |

`message.outbound_status` segue igual, com `provider_message_id` e `provider_accepted_at`; para
grupos, `ACCEPTED` significa aceito pelo provedor no grupo (não "lido por todos").

## 5. `/limits`

Se houver limites específicos de grupo (envios por minuto por grupo, tamanho máximo), expor em
`GET /api/v1/limits` junto de `idempotency_retention_seconds`. O bot usa isso para dimensionar a
política de resposta (ele também tem limites próprios por contato e por tenant).

## 6. Compatibilidade e versão

- Todos os campos são aditivos: `schema_version` continua `1`. Consumidores existentes devem ignorar
  campos que não conhecem (o `conversation_agent` ignora).
- Um `message.received` de chat direto ganha `chat_type: "direct"`, `mentioned: false`,
  `quoted_from_me` (conforme a citação). Nada mais muda para ele.
- Subscriptions existentes ficam com `include_groups: false`.

## 7. Critérios de aceite (o que o simulador e o gateway real devem provar)

1. Mensagem de chat direto: `chat_type: "direct"`, `from` = contato, sem `chat_id`, `mentioned: false`.
2. Mensagem em grupo sem menção: `chat_type: "group"`, `chat_id` = grupo, `from` = autor, `mentioned: false`.
3. Mensagem em grupo mencionando a instância: `mentioned: true` (testar com número E com LID/JID).
4. Mensagem em grupo que cita uma mensagem enviada pela instância: `quoted_from_me: true`.
5. Mensagem em grupo que cita mensagem de outra pessoa: `quoted_from_me: false`.
6. Subscription com `include_groups: false`: nenhuma mensagem de grupo é entregue (e é reconhecida).
7. Evento de sistema do grupo não vira `message.received`.
8. Envio para grupo com `reply_to_provider_message_id` e `mentions`: marca a pessoa certa; repetir com a
   mesma `Idempotency-Key` não duplica a mensagem no grupo.
9. A assinatura (`X-RelayPlane-*`) cobre o corpo inteiro, incluindo os campos novos.

## 8. Perguntas em aberto para o gateway responder

- Como a instância descobre o próprio id (número, LID, JID) e o que acontece quando o provedor troca de
  formato no meio da vida da instância?
- O volume de um grupo grande cabe no modelo de entrega at-least-once com `sequence` sem lacunas?
  `sequence` é por instância ou por chat?
- Para quais casos o gateway devolve `chat_type: "other"`?
- Quem remove a instância de um grupo: há evento para isso (`group.removed`)?
