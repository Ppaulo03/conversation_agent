"""Fetches inbound attachments from the gateway (claim-check): `GET /api/v1/media/{id}/content`.

Only a `ready` reference is fetched; the size is bounded while streaming, and the body must match
the checksum the gateway reports (`ETag`), so a truncated or altered download is an error, never
a silent partial file.

The request goes through the same `GuardedTransport` as every HTTP tool: HTTPS required, the host
checked against the connection, DNS resolved once and the address PINNED (no rebinding between the
check and the connect), private and reserved networks refused unless the operator allowed them, no
redirects. The media id comes from an event payload, so it is only ever put in the path as one
plain segment: never `..`, never a separator, never anything that could address another endpoint.
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import quote

import httpx

from conversation_agent.adapters.tools.guard import GuardedTransport, HostResolver, Rejected
from conversation_agent.core.models.media import MediaReference
from conversation_agent.ports.connections import ConnectionResolver
from conversation_agent.ports.secrets import SecretProvider

_MEDIA_ID = re.compile(r"[A-Za-z0-9._~-]{1,200}")


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
        resolve_host: HostResolver | None = None,
    ) -> None:
        self._guard = GuardedTransport(connections, secrets, client, resolve_host)
        self._connection_id = connection_id

    async def aclose(self) -> None:
        await self._guard.aclose()

    async def fetch(self, tenant_id: str, media: MediaReference, *, max_bytes: int) -> bytes:
        if media.status != "ready":
            raise MediaUnavailableError(f"media {media.media_id} is {media.status}")
        if media.size_bytes is not None and media.size_bytes > max_bytes:
            raise MediaUnavailableError("media is larger than the allowed size")
        if not _MEDIA_ID.fullmatch(media.media_id) or media.media_id in (".", ".."):
            raise MediaUnavailableError("media id is not acceptable")
        try:
            return await self._download(tenant_id, media, max_bytes)
        except Rejected as exc:  # not sent: the destination, the connection or the credential
            code = exc.result.error.code if exc.result.error else "REJECTED"
            raise MediaUnavailableError(f"media request refused ({code})") from exc

    async def _download(self, tenant_id: str, media: MediaReference, max_bytes: int) -> bytes:
        connection = await self._guard.connection(tenant_id, self._connection_id, None)
        segment = quote(media.media_id, safe="")
        url = httpx.URL(f"{connection.base_url.rstrip('/')}/api/v1/media/{segment}/content")
        request_url, host_header, sni = await self._guard.pin(url, connection)
        headers: dict[str, str] = {}
        if host_header is not None:
            headers["Host"] = host_header
        await self._guard.apply_auth(headers, connection, tenant_id)

        chunks: list[bytes] = []
        total = 0
        try:
            request = self._guard.client.build_request(
                "GET",
                request_url,
                headers=headers,
                timeout=httpx.Timeout(connection.max_timeout_seconds),
                extensions={"sni_hostname": sni} if sni else None,
            )
            response = await self._guard.client.send(request, stream=True, follow_redirects=False)
            try:
                if response.status_code != 200:
                    raise MediaUnavailableError(f"gateway answered {response.status_code}")
                expected = response.headers.get("ETag", "").strip('"')
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise MediaUnavailableError("media exceeds the allowed size")
                    chunks.append(chunk)
            finally:
                await response.aclose()
        except httpx.RequestError as exc:
            raise MediaUnavailableError("media download failed") from exc
        data = b"".join(chunks)
        if expected and hashlib.sha256(data).hexdigest() != expected:
            raise MediaUnavailableError("downloaded media does not match its checksum")
        return data
