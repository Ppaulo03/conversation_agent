-- Messages of one conversation leave in the order they were written, whatever worker sends them.
-- `conversation_seq` is assigned in the same transaction that writes the message, from a counter on
-- the conversation (so it does not depend on any worker's clock); a worker may only claim a message
-- whose predecessors are no longer waiting to be sent.
ALTER TABLE conversation_states ADD COLUMN outbound_seq bigint NOT NULL DEFAULT 0;
ALTER TABLE outbox_messages ADD COLUMN conversation_seq bigint;

WITH numbered AS (
    SELECT tenant_id, outbox_id,
           row_number() OVER (PARTITION BY tenant_id, conversation_id
                              ORDER BY created_at, message_index) AS n
      FROM outbox_messages
)
UPDATE outbox_messages o SET conversation_seq = numbered.n
  FROM numbered
 WHERE o.tenant_id = numbered.tenant_id AND o.outbox_id = numbered.outbox_id;

UPDATE conversation_states c
   SET outbound_seq = COALESCE((SELECT max(o.conversation_seq) FROM outbox_messages o
                                 WHERE o.tenant_id = c.tenant_id
                                   AND o.conversation_id = c.conversation_id), 0);

CREATE INDEX outbox_messages_by_conversation_seq
    ON outbox_messages (tenant_id, conversation_id, conversation_seq);
