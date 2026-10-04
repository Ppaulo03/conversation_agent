-- Phase 6 (real gateway contract): a send is asynchronous. The gateway answers with ITS message id
-- (`channel_message_id`) and only later reports the provider's id (`provider_message_id`, the one
-- a quoted reply carries) and the acceptance time. Inbound events remember the provider id of the
-- message they carry, so a "deleted" notice can withdraw an event that was not processed yet.
ALTER TABLE outbox_messages ADD COLUMN channel_message_id text;
CREATE INDEX outbox_messages_channel_id ON outbox_messages (tenant_id, channel_message_id)
    WHERE channel_message_id IS NOT NULL;
ALTER TABLE inbox_events ADD COLUMN provider_message_id text;
CREATE INDEX inbox_events_provider_id ON inbox_events (tenant_id, provider_message_id)
    WHERE provider_message_id IS NOT NULL;