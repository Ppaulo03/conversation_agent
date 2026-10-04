-- Phase 5.1: a conversation is pinned to (agent_id, agent_version), not to a bare version.
-- NULL = a conversation pinned before this column existed (treated as the routed agent).
ALTER TABLE conversation_states ADD COLUMN agent_id text;
