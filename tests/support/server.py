"""Runs an ASGI app on a real localhost socket so tests exercise real HTTP."""

from __future__ import annotations

import asyncio
import socket
import threading
import time

import uvicorn
from fastapi import FastAPI


class LiveServer:
    def __init__(self, app: FastAPI) -> None:
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self.port: int = self._sock.getsockname()[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
        self._thread = threading.Thread(
            target=lambda: asyncio.run(self._server.serve(sockets=[self._sock])), daemon=True
        )

    def start(self) -> None:
        self._thread.start()
        deadline = time.monotonic() + 10
        while not self._server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("test server did not start")
            time.sleep(0.01)

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10)
        self._sock.close()


def unused_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
