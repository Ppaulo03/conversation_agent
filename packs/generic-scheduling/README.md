# generic-scheduling

Book an appointment for a service on a day and time: collect what is missing, search the real
availability, let the contact pick one of the real options, then propose the booking for
confirmation. Conversation language: pt-BR. Version 1.0.0.

It is the scheduling behavior proven in the vertical slice, extracted. It contributes capability
contracts (`scheduling.availability`, `scheduling.create`, `scheduling.lookup_booking`), the
`scheduling` Flow, behavior rules for the persona and three evals. It contributes NO tools,
connections or bindings: the installing agent reaches its own scheduling system.

## What the installing agent provides

- `parameters`: `services` (id -> `keywords`, `duration_minutes`), `service_question`, optional `extra_triggers`;
- a binding for each of the three capabilities, whose tools satisfy `requires`:
  `scheduling.create` must be an idempotent tool recovered by `status_lookup` through
  `scheduling.lookup_booking`;
- `allow: [scheduling.availability, scheduling.create]` (the Pack never exposes anything by itself);
- for the evals, the variable `service_word`.

See `examples/pack-hosts/clinic.yaml` and `studio.yaml`: the same Pack over two different APIs.

```bash
uv run python -m conversation_agent.app.compile examples/pack-hosts/studio.yaml --packs packs
```
