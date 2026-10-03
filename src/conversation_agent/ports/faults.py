from __future__ import annotations

from typing import Protocol


class FaultInjector(Protocol):
    """Named crash points (chaos cases C01-C16). Production uses a no-op implementation."""

    async def hit(self, point: str) -> None: ...
