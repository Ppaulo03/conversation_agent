-- A resend the reconciler authorised is only valid until the channel may forget the idempotency
-- key. The authorisation carries its own deadline: a worker that wakes up later must not send.
ALTER TABLE outbox_messages ADD COLUMN resend_authorized_until timestamptz;
