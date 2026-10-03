from __future__ import annotations

from collections.abc import Iterator

import pytest

from scheduling_api.main import create_app
from support.builders import NOW
from support.server import LiveServer


@pytest.fixture(scope="session")
def _live_api() -> Iterator[tuple[LiveServer, object]]:
    app = create_app(now=lambda: NOW)
    server = LiveServer(app)
    server.start()
    yield server, app
    server.stop()


class ApiHandle:
    """Per-test view of the live reference API (state reset before each test)."""

    def __init__(self, server: LiveServer, app: object) -> None:
        self.base_url = server.base_url
        self.state = app.state  # type: ignore[attr-defined]

    @property
    def requests(self) -> list[dict[str, object]]:
        return list(self.state.requests)

    def availability_requests(self) -> list[dict[str, object]]:
        return [r for r in self.requests if r["path"] == "/availability"]


@pytest.fixture
def api(_live_api: tuple[LiveServer, object]) -> ApiHandle:
    server, app = _live_api
    handle = ApiHandle(server, app)
    handle.state.requests.clear()
    handle.state.fault = None
    handle.state.fully_booked_dates = set()
    handle.state.taken_slots = set()
    handle.state.bookings.clear()
    handle.state.by_key.clear()
    return handle
