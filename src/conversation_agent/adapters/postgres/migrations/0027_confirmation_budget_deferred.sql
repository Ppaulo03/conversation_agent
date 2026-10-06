ALTER TABLE action_confirmations DROP CONSTRAINT action_confirmations_interpreter_check;
ALTER TABLE action_confirmations
    ADD CONSTRAINT action_confirmations_interpreter_check
    CHECK (interpreter IN ('rule', 'llm', 'budget_deferred'));
