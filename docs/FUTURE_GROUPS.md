# Futuro: grupos, menções e quem pode falar com o bot

> **Status: FUTURO. Nada disto está implementado.** Este documento registra a decisão de desenho para
> quando for priorizado. O pedido ao gateway está em
> [`RELAYPLANE_GROUPS_SPEC.md`](./RELAYPLANE_GROUPS_SPEC.md); o que existe hoje está descrito abaixo.

## 1. Onde estamos

- O bot só atende conversa 1:1. O webhook do RelayPlane responde `200 ignored (reason: group)` a
  qualquer mensagem com `chat_id`; ela não vira `InboundEvent`.
- O `InboundEvent` não tem tipo de chat, remetente dentro do grupo nem menção. A identidade da conversa
  é `{instance_id}:{from}`.
- Não há allowlist/blocklist de contatos. `Subscription.instance_ids` filtra instâncias do gateway;
  `AdmissionControl` limita taxa; `ChannelPolicy` governa mensagens proativas. `allowed_capabilities`
  e `ToolAllowlist` são sobre ferramentas, não sobre pessoas.

## 2. Divisão de responsabilidades

| | RelayPlane (gateway) | conversation_agent (bot) |
|---|---|---|
| Tipo de chat, autor, menção, citação | **fornece** como fatos | confia, nunca deduz do texto |
| Quem pode falar com o bot (allowlist/blocklist) | n/a | **decide** (política do operador, na assinatura) |
| Quando responder em grupo | n/a | **decide** (política por assinatura/agente) |
| Limites | protege o provedor | protege custo e agente |

Princípio: fato do canal vem do gateway; política vem do bot; nada do que o contato escreve decide
nenhum dos dois (INV-002).

## 3. Desenho no bot (em ordem de custo)

### 3.1 Allowlist e blocklist por assinatura (independe do gateway)

`Subscription` ganha `allowed_contacts` e `blocked_contacts` (números normalizados). A checagem é no
webhook, **antes de persistir**: quem não passa recebe `200 ignored (reason: contact_not_allowed)`; o
gateway não reenvia e nada chega ao agente. É decisão do operador, vive na assinatura (o registro que o
operador controla), nunca no manifest do agente nem no payload.

Pontos a decidir: lista vazia significa "todos" ou "ninguém"; normalização do número (E.164); como
alterar a lista sem redeploy (o resolver de assinaturas já é uma porta).

### 3.2 Grupos como conversa de primeira classe

`InboundEvent` ganha, todos preenchidos pelo webhook a partir do que o gateway entrega:

| Campo | Significado |
|---|---|
| `conversation_kind` | `direct` \| `group` |
| `sender_id` | quem escreveu (em direto = `contact_id`) |
| `mentioned` | a instância foi marcada |
| `addressed` | `mentioned` ou citou uma mensagem do bot (`quoted_from_me`) |

A conversa de grupo tem identidade própria: `{instance_id}:{chat_id}`, com `contact_id` = o grupo e
`sender_id` por mensagem. Cada mensagem de grupo tem um autor; a conversa é compartilhada.

A assinatura declara a política de grupos (padrão **desligado**, como hoje):

| `groups` | Comportamento |
|---|---|
| `off` | grupos descartados (hoje) |
| `mention_only` | só gera turno quando `addressed`; o resto é ignorado |
| `allowlist` | só para `allowed_groups`; combinável com `mention_only` |

Mensagens de grupo que NÃO geram turno são descartadas na borda por padrão (não há "ouvir para
contexto" na primeira versão: seria armazenar conversa de terceiros sem que o bot tenha sido chamado,
com implicação de LGPD que precisa de decisão própria).

### 3.3 Interações que exigem cuidado (decidir antes de implementar)

- **Confirmação em grupo:** uma `PendingAction` pertence a quem a pediu. A elegibilidade do "sim" passa a
  exigir `sender_id` igual ao do solicitante; o "sim" de outro participante nunca confirma. O prompt
  cita o solicitante.
- **Handoff e erasure:** handoff de um grupo inteiro? Erasure de um participante apaga só as mensagens
  dele? Hoje erasure é por contato; em grupo o contato é o autor de cada mensagem.
- **Histórico e prompt:** o histórico do grupo mistura autores; o prompt precisa dizer quem falou (fato
  confiável do runtime, nunca o texto) para o modelo não confundir pessoas.
- **Mídia:** a mídia de um grupo tem dono (o autor); retenção e erasure seguem o autor.
- **Rate limit:** por grupo e por participante, além de por tenant.
- **Resposta:** em grupo o bot cita a mensagem que o chamou e marca o autor (`reply_to` e `mentions` no
  envio, ver a spec do gateway), para não responder "no ar".
- **Custo:** um grupo ativo multiplica turnos; o orçamento de LLM por agente/tenant precisa cobrir isso.

## 4. Dependências

1. O gateway entrega os campos da spec (`chat_type`, `from` = autor, `mentioned`, `quoted_from_me`) e a
   entrega opt-in de grupos (`include_groups`). Sem isso 3.2 não é seguro.
2. 3.1 (allowlist/blocklist) não depende do gateway e pode vir antes e separado.

## 5. Fora de escopo

Comunidades, canais e listas de transmissão (`chat_type: other`); chamadas; enquetes; reações como
entrada do agente.
