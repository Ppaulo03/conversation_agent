from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from agenda_api.main import create_app as create_agenda_app
from reference_mcp.main import DEFAULT_TOOLS as MCP_DEFAULT_TOOLS
from reference_mcp.main import PROTOCOL_VERSION as MCP_PROTOCOL_VERSION
from reference_mcp.main import create_app as create_mcp_app
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


class McpHandle:
    """Per-test view of the reference MCP server."""

    def __init__(self, server: LiveServer, app: object) -> None:
        self.base_url = f"{server.base_url}/mcp"
        self.state = app.state  # type: ignore[attr-defined]

    @property
    def calls(self) -> list[tuple[str, dict[str, object]]]:
        """Every `tools/call` that reached the server."""
        return list(self.state.calls)

    def methods(self) -> list[str]:
        return [str(r["path"]) for r in self.state.requests]


@pytest.fixture(scope="session")
def _live_mcp() -> Iterator[tuple[LiveServer, object]]:
    app = create_mcp_app()
    server = LiveServer(app)
    server.start()
    yield server, app
    server.stop()


@pytest.fixture
def mcp(_live_mcp: tuple[LiveServer, object]) -> McpHandle:
    server, app = _live_mcp
    handle = McpHandle(server, app)
    state = handle.state
    state.requests.clear()
    state.fault = None
    state.sessions = set()
    state.tools = [dict(t) for t in MCP_DEFAULT_TOOLS]
    state.calls.clear()
    state.tickets.clear()
    state.sse = False
    state.page_size = 0
    state.next_result = None
    state.require_session = True
    state.call_fault = None
    state.protocol_version = MCP_PROTOCOL_VERSION
    return handle


class AgendaHandle:
    """Per-test view of the SECOND scheduling API (a different vocabulary, on purpose)."""

    def __init__(self, server: LiveServer, app: object) -> None:
        self.base_url = server.base_url
        self.state = app.state  # type: ignore[attr-defined]

    @property
    def requests(self) -> list[dict[str, object]]:
        return list(self.state.requests)

    def horarios_requests(self) -> list[dict[str, object]]:
        return [r for r in self.requests if r["path"] == "/v2/agenda/horarios"]


@pytest.fixture(scope="session")
def _live_agenda() -> Iterator[tuple[LiveServer, object]]:
    app = create_agenda_app(now=lambda: NOW)
    server = LiveServer(app)
    server.start()
    yield server, app
    server.stop()


@pytest.fixture
def agenda(_live_agenda: tuple[LiveServer, object]) -> AgendaHandle:
    server, app = _live_agenda
    handle = AgendaHandle(server, app)
    state = handle.state
    state.requests.clear()
    state.fault = None
    state.taken_slots = set()
    state.reservas.clear()
    state.by_key.clear()
    return handle


@pytest.fixture(autouse=True)
def _pseudonym_key() -> Iterator[None]:
    """Every test runs with a pseudonymisation key (production refuses to run without one)."""
    from conversation_agent.core.models.audit import configure_pseudonym_key

    configure_pseudonym_key("test-pseudonym-key-0123456789abcdef-test")
    yield
    configure_pseudonym_key(None)
