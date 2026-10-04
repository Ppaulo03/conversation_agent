from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from relayplane_sim.main import create_app as create_relay_app
from relayplane_sim.main import settle as relay_settle
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


RELAY_T0 = datetime(2026, 10, 5, 11, 0, tzinfo=UTC)  # 08:00 in Sao Paulo, like NOW
RELAY_RETENTION = timedelta(hours=24)  # the gateway's default IDEMPOTENCY_TTL


class RelayHandle:
    """Per-test view of the simulated channel gateway; its clock is under test control."""

    def __init__(self, server: LiveServer, app: object, clock: dict[str, datetime]) -> None:
        self.base_url = server.base_url
        self.app = app
        self.state = app.state  # type: ignore[attr-defined]
        self._clock = clock

    def set_time(self, moment: datetime) -> None:
        self._clock["now"] = moment

    def advance(self, delta: timedelta) -> None:
        self._clock["now"] += delta

    @property
    def sent(self) -> list[dict[str, object]]:
        """What the gateway really created (deduped), one entry per message."""
        return list(self.state.sent)

    def settle(self, message_id: str, status: str, provider_message_id: str | None = None) -> None:
        """The provider reported what became of a message."""
        relay_settle(self.app, message_id, status, provider_message_id=provider_message_id)  # type: ignore[arg-type]


@pytest.fixture(scope="session")
def _live_relay() -> Iterator[tuple[LiveServer, object, dict[str, datetime]]]:
    clock = {"now": RELAY_T0}
    app = create_relay_app(now=lambda: clock["now"], idempotency_retention=RELAY_RETENTION)
    server = LiveServer(app)
    server.start()
    yield server, app, clock
    server.stop()


@pytest.fixture
def relay(_live_relay: tuple[LiveServer, object, dict[str, datetime]]) -> RelayHandle:
    server, app, clock = _live_relay
    clock["now"] = RELAY_T0
    handle = RelayHandle(server, app, clock)
    state = handle.state
    state.requests.clear()
    state.fault = None
    state.auto_accept = False
    state.messages.clear()
    state.by_key.clear()
    state.sent.clear()
    state.media.clear()
    state.media_etag.clear()
    return handle
