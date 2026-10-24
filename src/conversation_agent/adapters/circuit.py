"""The circuit-breaker state machine, shared by every outbound adapter that wants one.

closed -> open after `failure_threshold` consecutive health failures; open refuses without calling
for `open_seconds`; then half_open lets `half_open_probes` calls through: a healthy one closes the
circuit, a failed one opens it again. The caller decides what counts as a health failure (a tool
provider and a tool selector read their answers differently). State is in memory, per process, per
key (typically `(tenant, connection)`: one tenant's broken system does not stop another's).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Hashable
from dataclasses import dataclass
from typing import Literal

Admission = Literal["pass", "probe", "refuse"]


@dataclass
class _Circuit:
    state: str = "closed"
    failures: int = 0
    opened_at: float = 0.0
    probes: int = 0


class CircuitBreaker:
    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        open_seconds: float = 30.0,
        half_open_probes: int = 1,
        on_state: Callable[[Hashable, str], None] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1 or half_open_probes < 1 or open_seconds <= 0:
            raise ValueError("the circuit needs a positive threshold, window and probe count")
        self._threshold = failure_threshold
        self._open_seconds = open_seconds
        self._probes = half_open_probes
        self._on_state = on_state
        self._now = monotonic
        self._circuits: dict[Hashable, _Circuit] = {}

    def state(self, key: Hashable) -> str:
        return self._circuits.get(key, _Circuit()).state

    def admit(self, key: Hashable) -> Admission:
        """`refuse`: do not call. `probe`: call, and the verdict decides the circuit. A caller
        that got `pass` or `probe` MUST report the outcome with `settle`."""
        circuit = self._circuits.setdefault(key, _Circuit())
        if circuit.state == "open":
            if self._now() - circuit.opened_at < self._open_seconds:
                return "refuse"
            self._move(key, circuit, "half_open")
            circuit.probes = 0
        if circuit.state == "half_open":
            if circuit.probes >= self._probes:
                return "refuse"
            circuit.probes += 1
            return "probe"
        return "pass"

    def settle(self, key: Hashable, *, healthy: bool, probing: bool) -> None:
        circuit = self._circuits.setdefault(key, _Circuit())
        if healthy:
            circuit.failures = 0
            if probing and circuit.state == "half_open":
                self._move(key, circuit, "closed")
            return
        circuit.failures += 1
        if probing or circuit.failures >= self._threshold:
            circuit.opened_at = self._now()
            if circuit.state != "open":
                self._move(key, circuit, "open")

    def _move(self, key: Hashable, circuit: _Circuit, state: str) -> None:
        circuit.state = state
        if state == "closed":
            circuit.failures = 0
        if self._on_state is not None:
            try:
                self._on_state(key, state)
            except Exception:  # metrics must never change what a call does
                return
