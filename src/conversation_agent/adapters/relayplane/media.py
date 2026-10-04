"""Fetches inbound attachments from the gateway (claim-check): `GET /api/v1/media/{id}/content`.

Only a `ready` reference is fetched; the size is bounded while streaming, and the body must match
the checksum the gateway reports (`ETag`), so a truncated or altered download is an error, never
a silent partial file.
"""

from __future__ import annotations

import hashlib

import httpx

from conversation_agent.core.errors import ConnectionNotFoundError, SecretNotFoundError
from conversation_agent.core.models.media import MediaReference
from conversation_agent.ports.connections import ConnectionResolver
from conversation_agent.ports.secrets import SecretProvider


class MediaUnavailableError(Exception):
    """The attachment could not be retrieved intact."""


class RelayPlaneMediaFetcher:
    def __init__(
        self,
        connections: ConnectionResolver,
        secrets: SecretProvider | None = None,
        client: httpx.AsyncClient | None = None,
        *,
        connection_id: str = "relayplane",
    ) -> None:
        self._connections = connections
        self._secrets = secrets
        self._client = client or httpx.AsyncClient(follow_redirects=False)
        self._connection_id = connection_id

    async def fetch(self, tenant_id: str, media: MediaReference, *, max_bytes: int) -> bytes:
        if media.status != "ready":
            raise MediaUnavailableError(f"media {media.media_id} is {media.status}")
        if media.size_bytes is not None and media.size_bytes > max_bytes:
            raise MediaUnavailableError("media is larger than the allowed size")
        try:
            connection = await self._connections.resolve(tenant_id, self._connection_id)
        except ConnectionNotFoundError as exc:
            raise MediaUnavailableError("no gateway connection") from exc
        headers: dict[str, str] = {}
        if connection.auth is not None:
            if self._secrets is None:
                raise MediaUnavailableError("no credential available")
            try:
                secret = (await self._secrets.get(tenant_id, connection.auth.secret_ref)).reveal()
            except SecretNotFoundError as exc:
                raise MediaUnavailableError("no credential available") from exc
            auth = connection.auth
            headers[auth.header] = f"{auth.scheme} {secret}" if auth.scheme else secret
        url = f"{connection.base_url.rstrip('/')}/api/v1/media/{media.media_id}/content"
        chunks: list[bytes] = []
        total = 0
        try:
            async with self._client.stream(
                "GET", url, headers=headers, timeout=connection.max_timeout_seconds
            ) as response:
                if response.status_code != 200:
                    raise MediaUnavailableError(f"gateway answered {response.status_code}")
                expected = response.headers.get("ETag", "").strip('"')
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise MediaUnavailableError("media exceeds the allowed size")
                    chunks.append(chunk)
        except httpx.RequestError as exc:
            raise MediaUnavailableError("media download failed") from exc
        data = b"".join(chunks)
        if expected and hashlib.sha256(data).hexdigest() != expected:
            raise MediaUnavailableError("downloaded media does not match its checksum")
        return data
