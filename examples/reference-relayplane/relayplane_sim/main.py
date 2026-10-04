"""Simulator of the RelayPlane gateway, following ITS published contract
(github.com/Ppaulo03/RelayPlane: docs/CONTRACT.md, docs/EVENTS.md, docs/openapi.yaml).

Only what the framework's adapters use: asynchronous sends with Idempotency-Key replay and a
retention window, message status, `resolve`, `limits`, media download with checksum, and the
signed webhook envelope. It is NOT the real gateway; the opt-in live contract test is what checks
the same assumptions against a deployed one.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI, Header, Request, Response
from fastapi.responses import JSONResponse


def sign(secret: str, timestamp: int, body: bytes) -> str:
    """HMAC-SHA256 over "<timestamp>.<body>" (hex), as the gateway signs deliveries."""
    return hmac.new(
        secret.encode(), str(timestamp).encode() + b"." + body, hashlib.sha256
    ).hexdigest()


def webhook_headers(
    secret: str, body: bytes, timestamp: int, event_id: str, *extra_secrets: str
) -> dict[str, str]:
    """The headers of a delivery; several `v1=` entries while a secret rotates."""
    signatures = ",".join(f"v1={sign(s, timestamp, body)}" for s in (secret, *extra_secrets))
    return {
        "X-RelayPlane-Timestamp": str(timestamp),
        "X-RelayPlane-Signature": signatures,
        "X-RelayPlane-Event-Id": event_id,
    }


def envelope(
    event_type: str,
    payload: dict[str, Any],
    *,
    event_id: str,
    sequence: int | None = 1,
    tenant_id: str = "tenant_relay",
    instance_id: str = "inst_1",
    timestamp: datetime | None = None,
    schema_version: int = 1,
) -> dict[str, Any]:
    return {
        "schema_version": schema_version,
        "event_id": event_id,
        "sequence": sequence,
        "event_type": event_type,
        "provider": "evolution-v2",
        "tenant_id": tenant_id,
        "instance_id": instance_id,
        "timestamp": (timestamp or datetime.now(UTC)).isoformat().replace("+00:00", "Z"),
        "payload": payload,
    }


def message_received(
    event_id: str,
    *,
    sender: str = "5511999990000",
    text: str | None = "oi",
    msg_type: str = "text",
    provider_message_id: str | None = None,
    reply_to: str | None = None,
    media: dict[str, Any] | None = None,
    chat_id: str | None = None,
    **kw: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "provider_message_id": provider_message_id or f"3EB{event_id}",
        "from": sender,
        "push_name": "Ana",
        "type": msg_type,
    }
    if text is not None:
        payload["text"] = text
    if reply_to is not None:
        payload["reply_to_provider_message_id"] = reply_to
    if media is not None:
        payload["media"] = media
    if chat_id is not None:
        payload["chat_id"] = chat_id
    return envelope("message.received", payload, event_id=event_id, **kw)


def _error(status: int, code: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"code": code, "message": code})


def create_app(
    *,
    now: Callable[[], datetime] | None = None,
    idempotency_retention: timedelta = timedelta(hours=24),
) -> FastAPI:
    clock = now or (lambda: datetime.now(UTC))
    app = FastAPI(title="reference-relayplane")
    app.state.requests = []
    app.state.fault = None
    app.state.messages = {}  # message_id -> message
    app.state.by_key = {}  # idempotency key -> (fingerprint, message id, first seen)
    app.state.sent = []  # every message the gateway really created
    app.state.media = {}  # media_id -> bytes
    app.state.media_etag = {}  # media_id -> a checksum to LIE with (tests of a corrupted download)
    app.state.auto_accept = (
        False  # True: a created message is ACCEPTED at once (answer still QUEUED)
    )

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
        if fault and request.url.path != "/health":
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
            app.state.by_key.pop(key)  # the gateway FORGETS the key: a resend would DUPLICATE
            return None
        return entry  # type: ignore[no-any-return]

    def public(message: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in message.items() if k != "idempotency_key"}

    @app.post("/api/v1/messages/send")
    async def send(
        request: Request, idempotency_key: str | None = Header(default=None)
    ) -> Response:
        body = await request.json()
        if not isinstance(body, dict) or not {"instance_id", "to", "type", "payload"} <= set(body):
            return _error(400, "INVALID_REQUEST")
        fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        if idempotency_key:
            known = remembered(idempotency_key)
            if known is not None:
                if known[0] != fingerprint:
                    return _error(422, "IDEMPOTENCY_KEY_REUSED")  # same key, another payload
                return JSONResponse(
                    status_code=202,
                    headers={"Idempotent-Replayed": "true"},
                    content={"message_id": known[1], "status": "QUEUED"},
                )
        message_id = f"msg_{len(app.state.messages) + 1}"
        message: dict[str, Any] = {
            "id": message_id,
            "status": "QUEUED",
            "to": body["to"],
            "type": body["type"],
            "attempts": 0,
            "provider_message_id": None,
            "accepted_at": None,
            "idempotency_key": idempotency_key,
        }
        app.state.messages[message_id] = message
        if idempotency_key:
            app.state.by_key[idempotency_key] = (fingerprint, message_id, clock())
        app.state.sent.append(
            {**body, "message_id": message_id, "idempotency_key": idempotency_key}
        )
        if app.state.auto_accept:
            settle(app, message_id, "ACCEPTED", provider_message_id=f"3EBPROV{len(app.state.sent)}")
        return JSONResponse(status_code=202, content={"message_id": message_id, "status": "QUEUED"})

    @app.get("/api/v1/messages/{message_id}")
    async def get_message(message_id: str) -> Response:
        message = app.state.messages.get(message_id)
        if message is None:
            return _error(404, "NOT_FOUND")
        return JSONResponse(content=public(message))

    @app.post("/api/v1/messages/{message_id}/resolve")
    async def resolve(message_id: str, request: Request) -> Response:
        message = app.state.messages.get(message_id)
        if message is None:
            return _error(404, "NOT_FOUND")
        if message["status"] != "UNKNOWN":
            return _error(409, "NOT_UNKNOWN")
        outcome = (await request.json()).get("outcome")
        settle(
            app,
            message_id,
            "ACCEPTED" if outcome == "sent" else "FAILED",
            provider_message_id="3EBRESOLVED" if outcome == "sent" else None,
        )
        return JSONResponse(content=public(message))

    @app.get("/api/v1/limits")
    async def limits() -> dict[str, Any]:
        return {
            "idempotency_retention_seconds": int(idempotency_retention.total_seconds()),
            "max_text_length": 4096,
            "media": {"max_bytes": 25 * 1024 * 1024, "inbound_max_bytes": 25 * 1024 * 1024},
        }

    @app.get("/api/v1/media/{media_id}/content")
    async def media_content(media_id: str) -> Response:
        data = app.state.media.get(media_id)
        if data is None:
            return _error(404, "NOT_FOUND")
        etag = app.state.media_etag.get(media_id) or hashlib.sha256(data).hexdigest()
        return Response(
            content=data,
            media_type="application/octet-stream",
            headers={"ETag": f'"{etag}"'},
        )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    app.state.clock = clock
    return app


def settle(
    app: FastAPI, message_id: str, status: str, *, provider_message_id: str | None = None
) -> None:
    """Test control: the provider reported what became of a message (as the gateway would)."""
    message = app.state.messages[message_id]
    message["status"] = status
    if provider_message_id:
        message["provider_message_id"] = provider_message_id
    if status == "ACCEPTED":
        message["accepted_at"] = app.state.clock().isoformat().replace("+00:00", "Z")
