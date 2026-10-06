"""Channel-independent ASGI host for a durable runtime deployment.

The host owns process lifecycle and routing, not a channel protocol. The deployment factory chooses
the sender and ingress adapter; RelayPlane, another gateway, or an operator-owned webhook are all
equally external to this module.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, MutableMapping
from dataclasses import dataclass
from typing import Any, Protocol

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]
Close = Callable[[], Awaitable[None]]
OPS_PATHS = frozenset({"/healthz", "/readyz", "/metrics"})

log = logging.getLogger("conversation_agent.app.asgi")


class RuntimeRunner(Protocol):
    async def run(self, stop: asyncio.Event, *, poll_interval: float = 0.5) -> None: ...


@dataclass(frozen=True)
class Deployment:
    """Resources built during ASGI startup and owned until shutdown."""

    runtime: RuntimeRunner
    ingress: ASGIApp
    operations: ASGIApp
    close: Close | None = None


DeploymentFactory = Callable[[], Awaitable[Deployment]]


async def _unavailable(send: Send, error: str) -> None:
    body = json.dumps({"status": "not_ready", "reason": error}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 503,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": body})


def runtime_app(factory: DeploymentFactory, *, poll_interval: float = 0.5) -> ASGIApp:
    """Build an ASGI app whose lifespan owns one durable runtime.

    The three operational paths are reserved for ``Deployment.operations``; every other HTTP path
    and protocol is delegated to ``Deployment.ingress``. The factory runs during ASGI startup, so
    opening the database and provider clients never requires async work at module import time.
    """
    if poll_interval <= 0:
        raise ValueError("poll_interval must be positive")

    deployment: Deployment | None = None
    stop: asyncio.Event | None = None
    worker: asyncio.Task[None] | None = None
    worker_error: BaseException | None = None

    def capture_worker_result(done: asyncio.Task[None]) -> None:
        nonlocal worker_error
        if done.cancelled():
            return
        worker_error = done.exception()
        if worker_error is not None:
            log.error(
                "runtime.worker_stopped",
                exc_info=(type(worker_error), worker_error, worker_error.__traceback__),
            )

    async def close_deployment() -> None:
        if deployment is not None and deployment.close is not None:
            await deployment.close()

    async def lifespan(receive: Receive, send: Send) -> None:
        nonlocal deployment, stop, worker
        message = await receive()
        if message["type"] != "lifespan.startup":
            await send({"type": "lifespan.startup.failed", "message": "expected startup"})
            return
        try:
            deployment = await factory()
            stop = asyncio.Event()
            worker = asyncio.create_task(deployment.runtime.run(stop, poll_interval=poll_interval))
            worker.add_done_callback(capture_worker_result)
            await asyncio.sleep(0)  # an immediately broken worker makes startup fail, not readiness
            if worker.done():
                await worker
                raise RuntimeError("runtime worker stopped during startup")
        except Exception as exc:
            try:
                await close_deployment()
            except Exception:
                log.exception("runtime.cleanup_failed")
            await send({"type": "lifespan.startup.failed", "message": type(exc).__name__})
            return
        await send({"type": "lifespan.startup.complete"})

        message = await receive()
        if message["type"] != "lifespan.shutdown":
            await send({"type": "lifespan.shutdown.failed", "message": "expected shutdown"})
            return
        assert stop is not None and worker is not None
        stop.set()
        failure: BaseException | None = None
        try:
            await worker
        except Exception as exc:
            failure = exc
        try:
            await close_deployment()
        except Exception as exc:
            failure = failure or exc
        if failure is not None:
            await send({"type": "lifespan.shutdown.failed", "message": type(failure).__name__})
            return
        await send({"type": "lifespan.shutdown.complete"})

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await lifespan(receive, send)
            return
        if deployment is None:
            await _unavailable(send, "runtime_not_started")
            return
        if (
            scope["type"] == "http"
            and scope["path"] == "/readyz"
            and (worker is None or worker.done() or worker_error is not None)
        ):
            await _unavailable(send, "runtime_stopped")
            return
        target = (
            deployment.operations
            if scope["type"] == "http" and scope["path"] in OPS_PATHS
            else deployment.ingress
        )
        await target(scope, receive, send)

    return app
