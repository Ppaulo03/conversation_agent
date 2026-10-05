-- Phase 14: several runtimes may share one database; each only claims the work of ITS scope
-- (usually its agent id). A conversation belongs to one scope, fixed at first contact.
-- NULL = unscoped: only an unscoped worker (tests, single-purpose scripts) sees it.
ALTER TABLE conversation_states ADD COLUMN scope text;
-- conversations already pinned to an agent belong to that agent's scope
UPDATE conversation_states SET scope = agent_id WHERE agent_id IS NOT NULL;
CREATE INDEX conversation_states_scope ON conversation_states (scope);
