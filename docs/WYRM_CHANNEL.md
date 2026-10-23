# Wyrm channel

`conversation_agent.adapters.wyrm` lets the Wyrm Systems core act as the channel (the relay plane)
for this framework. It has two parts and does not import `wyrm-sdk` (nor pika): the real
`WyrmPublisher` satisfies the `WyrmPublish` protocol structurally.

- `dispatch_to_event(dispatch, tenant_id=..., clock=...)` maps a dispatch dict to an `InboundEvent`
  (or `None`: nothing to hand to the runtime). Call it from the module worker that receives the
  dispatch, then `await runtime.receive(event)`.
- `WyrmSender(publisher)` is the `MessageSender`; `WyrmSender.delivery_policy()` is the
  `DeliveryPolicy` to pass to `Runtime.build`.

## What the adapter assumes

- The dispatch is already authenticated by the Wyrm SDK and `tenant_id` comes from your
  configuration; a dispatch for another tenant raises. Identity comes only from `context`.
- `event_id` is `<provider_id>:<chat_id>:<payload.message_id>`, so a redelivery dedupes (INV-020).
  `dispatch_id` is per dispatch and changes on a redelivery, so it is never used: a dispatch without
  a `message_id` is not mapped. `conversation_id` is `wyrm:<provider_id>:<chat_id>` (no tenant in it;
  ids are unique per tenant already); `channel_id` is `provider_id`. A `:` in `provider_id` or
  `chat_id` is refused by the mapper.
- `contact_id` is the `chat_id`, not `sender.user_id`: a reply is addressed to `(provider, chat)` and
  `OutboundMessage` carries only the contact. In a private chat the chat is the contact.
- `occurred_at` is the core's `timestamp` (missing or unparseable: receive time). `provider_occurred_at` and
  `reply_to_provider_message_id` stay `None`: the dispatch has no provider clock and no reply-to.
- Only `PRIVATE` chats are mapped; a `GROUP` dispatch returns `None`. So do a dispatch without text and
  one whose `provider_id`/`chat_id` contains `:` (Wyrm cannot address it either).
- The outbox `idempotency_key` is the Wyrm `event_id`. Success is `ACCEPTED` with no provider id and
  no `provider_accepted_at` (INV-026); a `ValueError` from the SDK is `FAILED`, not retryable; any other
  error escapes and the framework records `UNKNOWN`. Publishes are serialised on one thread (pika
  connections are not thread-safe); call `WyrmSender.close()` on shutdown. A cancellation during a
  publish is ambiguous (the message may still go out), so it is never `FAILED`.

## Limitations

1. **No provider evidence, so protected actions cannot be confirmed.** Confirmation needs a provider
   timestamp or a reply-to to prove the "yes" answers the prompt (INV-022, INV-026). Wyrm gives
   neither, so the runtime stays ambiguous and asks again. Keep protected actions disabled on this
   channel, or add confirmation evidence at the Wyrm core first.
2. **No outbound idempotency, so the retry horizon is 0.** The core does not dedupe outputs: the same
   `event_id` published twice is delivered twice. The policy is `DeliveryPolicy.no_retry(...)`
   (INV-034): a row is sent once, and an ambiguous send (broker error) stays `UNKNOWN`, never resent.
3. **Text only.** Attachments are ignored; a media-only dispatch is not mapped, and replies are text.
4. **`UNKNOWN` never settles.** There is no lookup and no status push over Wyrm. An `UNKNOWN` row
   (broker error, cancellation) is left as it is by the reconciler (horizon 0: never resent) and
   stays `UNKNOWN` for good. It holds back the next message of that conversation until the row's
   first send is older than the safe resend window (1 h retention - 1 min margin = 59 min); after
   that it stops blocking. Watch for `UNKNOWN` rows: only an operator can tell whether it went out.
