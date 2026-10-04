"""Simulator of the channel gateway (see README: an assumed contract, not the vendor's API)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI, Header, Query, Request, Response
from fastapi.responses import JSONResponse


def sign(secret: str, body: bytes, timestamp: int) -> str:
    """`t=<unix>,v1=<hex hmac-sha256 of "<t>.<body>">`: the header the gateway sends."""
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return f"t={timestamp},v1={mac.hexdigest()}"


def inbound_event(
    event_id: str,
    *,
    channel_id: str = "wa-1",
    conversation_id: str = "conv-1",
    contact_id: str = "contact-1",
    text: str | None = "oi",
    occurred_at: datetime | None = None,
    sequence: int | None = None,
    reply_to: str | None = None,
    media: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "id": event_id,
        "type": "message.received",
        "channel_id": channel_id,
        "conversation_id": conversation_id,
        "contact_id": contact_id,
        "occurred_at": (occurred_at or datetime.now(UTC)).isoformat(),
        "sequence": sequence,
        "reply_to": reply_to,
        "text": text,
        "media": media or [],
    }


def _error(status: int, code: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"code": code})


def create_app(
    *,
    now: Callable[[], datetime] | None = None,
    idempotency_retention: timedelta = timedelta(hours=1),
) -> FastAPI:
    clock = now or (lambda: datetime.now(UTC))
    app = FastAPI(title="reference-relayplane")
    app.state.requests = []
    app.state.fault = None
    app.state.queue_mode = False  # True: new messages are reported as `queued`, not `accepted`
    app.state.messages = {}  # id -> message
    app.state.by_key = {}  # idempotency key -> (fingerprint, message id, first seen)
    app.state.deliveries = []  # every message the gateway really created

    @app.middleware("http")
    async def observe_and_inject_faults(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        app.state.requests.append(
            {
                "method": request.method,
                "path": request.url.path,
                "query": dict(request.query_params),
                "headers": {k.lower(): v for k, v in request.headers.items()},
            }
        )
        fault: dict[str, Any] | None = app.state.fault
        if fault:
            if "delay" in fault:
                await asyncio.sleep(fault["delay"])
            if "status" in fault:
                return _error(fault["status"], "INJECTED_FAULT")
            response = await call_next(request)  # the effect HAPPENS...
            if "status_after_effect" in fault:  # ...but the caller only sees an error
                return _error(fault["status_after_effect"], "INJECTED_FAULT")
            return response
        return await call_next(request)

    def remembered(key: str) -> tuple[str, str, datetime] | None:
        entry = app.state.by_key.get(key)
        if entry is None:
            return None
        if clock() - entry[2] > idempotency_retention:
            app.state.by_key.pop(key)  # the gateway FORGETS the key: a resend would duplicate
            return None
        return entry  # type: ignore[no-any-return]

    @app.post("/v1/messages")
    async def send(
        request: Request, idempotency_key: str | None = Header(default=None)
    ) -> Response:
        if not idempotency_key:
            return _error(400, "IDEMPOTENCY_KEY_REQUIRED")
        body = await request.json()
        fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        known = remembered(idempotency_key)
        if known is not None:
            if known[0] != fingerprint:
                return _error(409, "IDEMPOTENCY_KEY_REUSED")
            return JSONResponse(status_code=200, content=app.state.messages[known[1]])
        message_id = f"msg_{len(app.state.messages) + 1}"
        message = {
            "id": message_id,
            "status": "queued" if app.state.queue_mode else "accepted",
            "accepted_at": None if app.state.queue_mode else clock().isoformat(),
            "idempotency_key": idempotency_key,
        }
        app.state.messages[message_id] = message
        app.state.by_key[idempotency_key] = (fingerprint, message_id, clock())
        app.state.deliveries.append({**body, "id": message_id, "idempotency_key": idempotency_key})
        return JSONResponse(status_code=202, content=message)

    @app.get("/v1/messages")
    async def lookup(idempotency_key: str = Query()) -> Response:
        known = remembered(idempotency_key)
        if known is None:
            return _error(404, "NOT_FOUND")
        return JSONResponse(content=app.state.messages[known[1]])

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
