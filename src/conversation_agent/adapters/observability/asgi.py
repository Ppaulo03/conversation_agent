"""Operational endpoints as a dependency-free ASGI app:

  GET /healthz   liveness: the process is up (always 200)
  GET /readyz    readiness: the database answers AND the schema is exactly what this build expects
  GET /metrics   Prometheus text: backlogs and ages of the durable queues, tool and circuit counters

Mount it on a port that is not public. It reveals counts and names, never content.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from conversation_agent.adapters.observability.prometheus import render
from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.adapters.postgres.health import collect_health
from conversation_agent.adapters.postgres.migrator import Migrator
from conversation_agent.adapters.tools.metrics import InMemoryToolMetrics
from conversation_agent.core.errors import ConversationAgentError

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]


def ops_app(
    db: PostgresDatabase, tools: InMemoryToolMetrics | None = None
) -> Callable[[Scope, Receive, Send], Awaitable[None]]:
    async def respond(send: Send, status: int, body: bytes, content_type: str) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", content_type.encode())],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def json_response(send: Send, status: int, payload: dict[str, Any]) -> None:
        await respond(send, status, json.dumps(payload).encode(), "application/json")

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return
        path, method = scope["path"], scope["method"]
        if method != "GET":
            await json_response(send, 405, {"error": "method_not_allowed"})
        elif path == "/healthz":
            await json_response(send, 200, {"status": "alive"})
        elif path == "/readyz":
            try:
                await Migrator(db.pool).ensure_current()
            except ConversationAgentError as exc:
                await json_response(
                    send, 503, {"status": "not_ready", "reason": type(exc).__name__}
                )
            except Exception as exc:  # the database itself is unreachable
                await json_response(
                    send, 503, {"status": "not_ready", "reason": type(exc).__name__}
                )
            else:
                await json_response(send, 200, {"status": "ready"})
        elif path == "/metrics":
            try:
                body = render(await collect_health(db), tools).encode()
            except Exception:
                await json_response(send, 503, {"error": "metrics_unavailable"})
            else:
                await respond(send, 200, body, "text/plain; version=0.0.4; charset=utf-8")
        else:
            await json_response(send, 404, {"error": "not_found"})

    return app
