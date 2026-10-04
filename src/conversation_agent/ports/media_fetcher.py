from __future__ import annotations

from typing import Protocol

from conversation_agent.core.models.media import MediaReference


class MediaFetcher(Protocol):
    async def fetch(self, tenant_id: str, media: MediaReference, *, max_bytes: int) -> bytes:
        """The bytes behind a claim-check reference. Raises when it cannot be retrieved intact
        (not found, too large, checksum mismatch): a Transcriber turns that into "unavailable"."""
        ...
