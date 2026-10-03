"""Reference scheduling API: an *external system* that owns the scheduling business state.

It deliberately knows nothing about conversation_agent (and vice-versa): the framework
reaches it only through Capability -> Binding -> HTTPToolProvider. Its own vocabulary
(service_code, items/start/end, pagination) differs from the capability schema on purpose,
so that bindings have real mapping work to do.

Phase 1 exposes availability only. Writes (POST /bookings, idempotency lookup, DELETE)
arrive with the reliability core (Phase 2).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import JSONResponse

TZ = ZoneInfo("America/Sao_Paulo")
SERVICES: dict[str, int] = {"HC-01": 30, "CN-01": 60}  # service_code -> duration in minutes
BUSINESS_WINDOWS = ((time(9, 0), time(12, 0)), (time(14, 0), time(18, 0)))
MAX_RANGE_DAYS = 14


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"code": code, "message": message})


def _demo_taken(slot_start: datetime) -> bool:
    """Deterministic 'someone already booked this' pattern so the demo has partial gaps."""
    return (slot_start.date().toordinal() + slot_start.hour) % 5 == 0


def create_app(
    *,
    now: Callable[[], datetime] | None = None,
    fully_booked_dates: set[date] | None = None,
    taken_slots: set[datetime] | None = None,
    demo_seed: bool = False,
) -> FastAPI:
    clock = now or (lambda: datetime.now(TZ))
    app = FastAPI(title="reference-scheduling-api")
    app.state.requests = []  # observed requests (test visibility only)
    app.state.fault = None  # {"status": 503} or {"delay": seconds}; test-only fault injection
    app.state.fully_booked_dates = fully_booked_dates or set()
    app.state.taken_slots = taken_slots or set()

    @app.middleware("http")
    async def observe_and_inject_faults(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        app.state.requests.append(
            {
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
                return _error(fault["status"], "INJECTED_FAULT", "injected fault")
        return await call_next(request)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/availability")
    async def availability(
        service_code: str,
        start: date,
        end: date,
        limit: int = Query(20, ge=1, le=50),
        cursor: str | None = None,
    ) -> Response:
        duration = SERVICES.get(service_code)
        if duration is None:
            return _error(404, "SERVICE_NOT_FOUND", f"unknown service {service_code}")
        if end < start or (end - start).days >= MAX_RANGE_DAYS:
            return _error(422, "INVALID_RANGE", "end must be >= start and within 14 days")
        try:
            offset = int(cursor) if cursor else 0
        except ValueError:
            return _error(422, "INVALID_CURSOR", "malformed cursor")

        current = clock()
        slots: list[tuple[datetime, datetime]] = []
        day = start
        while day <= end:
            if day.weekday() < 5 and day not in app.state.fully_booked_dates:
                for window_start, window_end in BUSINESS_WINDOWS:
                    slot_start = datetime.combine(day, window_start, tzinfo=TZ)
                    closing = datetime.combine(day, window_end, tzinfo=TZ)
                    while slot_start + timedelta(minutes=duration) <= closing:
                        slot_end = slot_start + timedelta(minutes=duration)
                        taken = slot_start in app.state.taken_slots or (
                            demo_seed and _demo_taken(slot_start)
                        )
                        if slot_start > current and not taken:
                            slots.append((slot_start, slot_end))
                        slot_start = slot_end
            day += timedelta(days=1)

        page = slots[offset : offset + limit]
        next_cursor = str(offset + limit) if offset + limit < len(slots) else None
        return JSONResponse(
            {
                "items": [{"start": s.isoformat(), "end": e.isoformat()} for s, e in page],
                "pagination": {"next_cursor": next_cursor},
            }
        )

    return app


app = create_app(demo_seed=True)
