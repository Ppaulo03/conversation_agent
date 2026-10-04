"""A dependency-free ASGI app around `RelayPlaneWebhook`: POST /webhooks/relayplane/{subscription}.

It reads the RAW body (the signature covers exact bytes), never parses before verifying, and
answers the status the handler decided. Mount it under any ASGI server.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from conversation_agent.adapters.relayplane.webhook import RelayPlaneWebhook

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]
PREFIX = "/webhooks/relayplane/"


def webhook_app(webhook: RelayPlaneWebhook) -> Callable[[Scope, Receive, Send], Awaitable[None]]:
    async def respond(send: Send, status: int, body: dict[str, Any]) -> None:
        payload = json.dumps(body).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": payload})

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return
        path: str = scope["path"]
        if scope["method"] != "POST" or not path.startswith(PREFIX) or "/" in path[len(PREFIX) :]:
            await respond(send, 404, {"error": "not_found"})
            return
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if len(body) > webhook._max_body:
                await respond(send, 413, {"error": "payload_too_large"})
                return
            if not message.get("more_body", False):
                break
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
        result = await webhook.handle(path[len(PREFIX) :], body, headers)
        await respond(send, result.status, result.body)

    return app
