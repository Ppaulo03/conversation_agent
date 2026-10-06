from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from conversation_agent.app.asgi import ASGIApp, Deployment, runtime_app

Message = MutableMapping[str, Any]


class Runtime:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()
        self.exit_early = asyncio.Event()

    async def run(self, stop: asyncio.Event, *, poll_interval: float = 0.5) -> None:
        self.started.set()
        stop_wait = asyncio.create_task(stop.wait())
        early_wait = asyncio.create_task(self.exit_early.wait())
        done, pending = await asyncio.wait(
            (stop_wait, early_wait), return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        assert done
        self.stopped.set()


def responder(name: str) -> ASGIApp:
    async def app(scope: Message, receive: Callable[[], Awaitable[Message]], send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": name.encode()})

    return app


async def request(app: ASGIApp, path: str) -> tuple[int, bytes]:
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    await app({"type": "http", "method": "GET", "path": path, "headers": []}, receive, send)
    return sent[0]["status"], sent[1]["body"]


async def start(app: ASGIApp) -> tuple[asyncio.Queue[Message], list[Message], asyncio.Task[None]]:
    incoming: asyncio.Queue[Message] = asyncio.Queue()
    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    task = asyncio.create_task(app({"type": "lifespan"}, incoming.get, send))
    await incoming.put({"type": "lifespan.startup"})
    while not sent:
        await asyncio.sleep(0)
    return incoming, sent, task


async def test_the_host_owns_runtime_lifecycle_and_keeps_the_channel_outside_the_core() -> None:
    runtime = Runtime()
    closed = asyncio.Event()

    async def close() -> None:
        closed.set()

    async def factory() -> Deployment:
        return Deployment(runtime, responder("channel"), responder("operations"), close)

    app = runtime_app(factory, poll_interval=0.01)
    incoming, sent, lifespan = await start(app)
    assert sent == [{"type": "lifespan.startup.complete"}]
    assert runtime.started.is_set()
    assert await request(app, "/healthz") == (200, b"operations")
    assert await request(app, "/readyz") == (200, b"operations")
    assert await request(app, "/metrics") == (200, b"operations")
    assert await request(app, "/webhooks/anything") == (200, b"channel")

    await incoming.put({"type": "lifespan.shutdown"})
    await lifespan
    assert sent[-1] == {"type": "lifespan.shutdown.complete"}
    assert runtime.stopped.is_set() and closed.is_set()


async def test_a_worker_that_stops_makes_readiness_fail() -> None:
    runtime = Runtime()

    async def factory() -> Deployment:
        return Deployment(runtime, responder("channel"), responder("operations"))

    app = runtime_app(factory)
    incoming, _, lifespan = await start(app)
    runtime.exit_early.set()
    await runtime.stopped.wait()
    await asyncio.sleep(0)

    status, body = await request(app, "/readyz")
    assert status == 503
    assert json.loads(body) == {"status": "not_ready", "reason": "runtime_stopped"}

    await incoming.put({"type": "lifespan.shutdown"})
    await lifespan
