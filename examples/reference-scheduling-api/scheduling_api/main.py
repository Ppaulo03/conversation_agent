"""Reference scheduling API: an *external system* that owns the scheduling business state.

It deliberately knows nothing about conversation_agent (and vice-versa): the framework
reaches it only through Capability -> Binding -> HTTPToolProvider. Its own vocabulary
(service_code, items/start/end, pagination, `state`) differs from the capability schemas on
purpose, so that bindings have real mapping work to do.

Endpoints: GET /availability, POST /bookings (idempotent by `Idempotency-Key`),
GET /bookings/by-idempotency-key/{key}, DELETE /bookings/{id}, GET /health.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
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
    app.state.fault = None  # test-only fault injection, see the middleware below
    app.state.fully_booked_dates = fully_booked_dates or set()
    app.state.taken_slots = taken_slots or set()
    app.state.bookings = {}  # booking_id -> booking dict (the business state lives HERE)
    app.state.by_key = {}  # idempotency key -> (fingerprint, booking_id)

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
            if "delay" in fault:  # never reaches the handler before the client gives up
                await asyncio.sleep(fault["delay"])
            if "status" in fault:  # fails before any effect
                return _error(fault["status"], "INJECTED_FAULT", "injected fault")
            response = await call_next(request)  # the effect HAPPENS...
            if "status_after_effect" in fault:  # ...but the caller only sees an error
                return _error(fault["status_after_effect"], "INJECTED_FAULT", "response lost")
            if "delay_after_effect" in fault:
                await asyncio.sleep(fault["delay_after_effect"])
            return response
        return await call_next(request)

    def free_slots(service_code: str, start: date, end: date) -> list[tuple[datetime, datetime]]:
        duration = SERVICES[service_code]
        current = clock()
        booked = [
            (b["start"], b["end"]) for b in app.state.bookings.values() if b["state"] == "confirmed"
        ]
        slots: list[tuple[datetime, datetime]] = []
        day = start
        while day <= end:
            if day.weekday() < 5 and day not in app.state.fully_booked_dates:
                for window_start, window_end in BUSINESS_WINDOWS:
                    slot_start = datetime.combine(day, window_start, tzinfo=TZ)
                    closing = datetime.combine(day, window_end, tzinfo=TZ)
                    while slot_start + timedelta(minutes=duration) <= closing:
                        slot_end = slot_start + timedelta(minutes=duration)
                        taken = (
                            slot_start in app.state.taken_slots
                            or (demo_seed and _demo_taken(slot_start))
                            or any(
                                slot_start < b_end and slot_end > b_start
                                for b_start, b_end in booked
                            )
                        )
                        if slot_start > current and not taken:
                            slots.append((slot_start, slot_end))
                        slot_start = slot_end
            day += timedelta(days=1)
        return slots

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
        if service_code not in SERVICES:
            return _error(404, "SERVICE_NOT_FOUND", f"unknown service {service_code}")
        if end < start or (end - start).days >= MAX_RANGE_DAYS:
            return _error(422, "INVALID_RANGE", "end must be >= start and within 14 days")
        try:
            offset = int(cursor) if cursor else 0
        except ValueError:
            return _error(422, "INVALID_CURSOR", "malformed cursor")

        slots = free_slots(service_code, start, end)
        page = slots[offset : offset + limit]
        next_cursor = str(offset + limit) if offset + limit < len(slots) else None
        return JSONResponse(
            {
                "items": [{"start": s.isoformat(), "end": e.isoformat()} for s, e in page],
                "pagination": {"next_cursor": next_cursor},
            }
        )

    def public(booking: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": booking["id"],
            "service_code": booking["service_code"],
            "starts_at": booking["start"].isoformat(),
            "state": booking["state"],
            "idempotency_key": booking["idempotency_key"],
        }

    @app.post("/bookings")
    async def create_booking(request: Request) -> Response:
        key = request.headers.get("idempotency-key")
        if not key:
            return _error(400, "IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key header is required")
        try:
            body = await request.json()
            service_code = str(body["service_code"])
            starts_at = datetime.fromisoformat(str(body["starts_at"])).astimezone(TZ)
            hours = float(body["hours"])
        except (ValueError, KeyError, TypeError):
            return _error(422, "INVALID_BODY", "service_code, starts_at and hours are required")

        fingerprint = hashlib.sha256(
            json.dumps([service_code, starts_at.isoformat(), hours]).encode()
        ).hexdigest()
        if key in app.state.by_key:  # idempotent replay
            stored_fingerprint, booking_id = app.state.by_key[key]
            if stored_fingerprint != fingerprint:
                return _error(422, "IDEMPOTENCY_KEY_REUSED", "key used with a different request")
            return JSONResponse(public(app.state.bookings[booking_id]), status_code=200)

        if service_code not in SERVICES or abs(hours * 60 - SERVICES[service_code]) > 1e-6:
            return _error(422, "INVALID_SERVICE_OR_DURATION", "unknown service or wrong duration")
        day = starts_at.date()
        if starts_at not in [s for s, _ in free_slots(service_code, day, day)]:
            return _error(409, "SLOT_UNAVAILABLE", "slot is not available")  # no double booking

        booking_id = f"bk_{len(app.state.bookings) + 1}"
        booking = {
            "id": booking_id,
            "service_code": service_code,
            "start": starts_at,
            "end": starts_at + timedelta(minutes=SERVICES[service_code]),
            "state": "confirmed",
            "idempotency_key": key,
        }
        app.state.bookings[booking_id] = booking
        app.state.by_key[key] = (fingerprint, booking_id)
        return JSONResponse(public(booking), status_code=201)

    @app.get("/bookings/by-idempotency-key/{key}")
    async def booking_by_key(key: str) -> Response:
        if key not in app.state.by_key:
            return _error(404, "BOOKING_NOT_FOUND", "no booking for this idempotency key")
        return JSONResponse(public(app.state.bookings[app.state.by_key[key][1]]))

    @app.delete("/bookings/{booking_id}")
    async def cancel_booking(booking_id: str) -> Response:
        booking = app.state.bookings.get(booking_id)
        if booking is None:
            return _error(404, "BOOKING_NOT_FOUND", "unknown booking")
        booking["state"] = "cancelled"
        return Response(status_code=204)

    return app


app = create_app(demo_seed=True)
