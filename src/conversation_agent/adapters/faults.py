"""Fault injection for chaos tests (ROADMAP "Chaos gates"). Production wires `NoFaults`."""

from __future__ import annotations


class SimulatedCrash(BaseException):
    """A process kill at a named point. Deliberately a BaseException so that no
    `except Exception` in the code under test can swallow it (a real kill is not catchable)."""

    def __init__(self, point: str) -> None:
        super().__init__(point)
        self.point = point


class NoFaults:
    async def hit(self, point: str) -> None:
        return None


class ChaosFaults:
    """Crashes the first time each armed point is reached (then disarms itself)."""

    def __init__(self, *points: str) -> None:
        self._armed = set(points)
        self.hits: list[str] = []

    def arm(self, *points: str) -> None:
        self._armed.update(points)

    async def hit(self, point: str) -> None:
        self.hits.append(point)
        if point in self._armed:
            self._armed.discard(point)
            raise SimulatedCrash(point)
