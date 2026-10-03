from __future__ import annotations

from datetime import datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Timezone-aware 'now'. Turns snapshot this once as `turn_reference_time`."""
        ...
