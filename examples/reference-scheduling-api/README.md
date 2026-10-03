# reference-scheduling-api

An **external system** used by the examples and tests. It owns scheduling business state;
`conversation_agent` never imports it (enforced by import-linter) and reaches it only through
`Capability -> Binding -> HTTPToolProvider`.

Run it:

```bash
uv run uvicorn scheduling_api.main:app --port 8001
```

Phase 1 endpoints:

| Method | Path | Notes |
|---|---|---|
| GET | `/health` | liveness |
| GET | `/availability?service_code=HC-01&start=2026-10-05&end=2026-10-09` | services `HC-01` (30 min), `CN-01` (60 min); Mon-Fri 09-12 / 14-18 (America/Sao_Paulo); `limit`, `cursor` pagination |

Errors: `404 SERVICE_NOT_FOUND`, `422 INVALID_RANGE`.

Writes (`POST /bookings`, lookup by idempotency key, `DELETE`) arrive with Phase 2.
