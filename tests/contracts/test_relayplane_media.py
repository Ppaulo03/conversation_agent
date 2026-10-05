"""RelayPlaneMediaFetcher against the gateway simulator: claim-check bytes, bounded and verified."""

from __future__ import annotations

import hashlib

import httpx
import pytest

from conftest import RelayHandle
from conversation_agent.adapters.connections.static import StaticConnectionResolver
from conversation_agent.adapters.relayplane.media import (
    MediaUnavailableError,
    RelayPlaneMediaFetcher,
)
from conversation_agent.core.models.connections import ResolvedConnection
from conversation_agent.core.models.media import MediaReference

DATA = b"OggS" + b"\x00" * 200


def fetcher(relay: RelayHandle) -> RelayPlaneMediaFetcher:
    connection = ResolvedConnection(
        connection_id="relayplane",
        base_url=relay.base_url,
        allow_private_networks=True,
        tls_required=False,
    )
    return RelayPlaneMediaFetcher(StaticConnectionResolver({"relayplane": connection}))


def ref(media_id: str = "med_1", **kw: object) -> MediaReference:
    return MediaReference(media_id=media_id, kind="audio", mime_type="audio/ogg", **kw)  # type: ignore[arg-type]


async def test_a_ready_attachment_is_downloaded_and_verified(relay: RelayHandle) -> None:
    relay.state.media["med_1"] = DATA
    assert await fetcher(relay).fetch("t", ref(), max_bytes=1000) == DATA


@pytest.mark.parametrize("status", ["rejected", "failed"])
async def test_only_a_ready_attachment_is_ever_requested(relay: RelayHandle, status: str) -> None:
    relay.state.media["med_1"] = DATA
    with pytest.raises(MediaUnavailableError, match=status):
        await fetcher(relay).fetch("t", ref(status=status), max_bytes=1000)  # type: ignore[arg-type]
    assert relay.state.requests == []


async def test_the_size_limit_holds_before_and_while_downloading(relay: RelayHandle) -> None:
    relay.state.media["med_1"] = DATA
    with pytest.raises(MediaUnavailableError, match="larger"):
        await fetcher(relay).fetch("t", ref(size_bytes=10_000), max_bytes=100)  # declared size
    assert relay.state.requests == []
    with pytest.raises(MediaUnavailableError, match="exceeds"):
        await fetcher(relay).fetch("t", ref(), max_bytes=100)  # the gateway's size was a lie


async def test_a_missing_attachment_is_unavailable(relay: RelayHandle) -> None:
    with pytest.raises(MediaUnavailableError, match="404"):
        await fetcher(relay).fetch("t", ref("nope"), max_bytes=1000)


async def test_bytes_that_do_not_match_the_checksum_are_never_returned(
    relay: RelayHandle,
) -> None:
    relay.state.media["med_1"] = DATA
    relay.state.media_etag = {"med_1": hashlib.sha256(b"something else").hexdigest()}
    with pytest.raises(MediaUnavailableError, match="checksum"):
        await fetcher(relay).fetch("t", ref(), max_bytes=1000)


# --- the same guards as every HTTP tool (INV-027 family) ---


def guarded(
    handler: object,
    *,
    base_url: str = "https://gateway.example",
    resolved: list[str] | None = None,
    private_ok: bool = False,
) -> tuple[RelayPlaneMediaFetcher, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)  # type: ignore[operator, no-any-return]

    async def resolve(host: str, port: int) -> list[str]:
        return resolved or ["93.184.216.34"]

    connection = ResolvedConnection(
        connection_id="relayplane",
        base_url=base_url,
        allow_private_networks=private_ok,
        tls_required=base_url.startswith("https"),
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(record))
    return (
        RelayPlaneMediaFetcher(
            StaticConnectionResolver({"relayplane": connection}),
            client=client,
            resolve_host=resolve,
        ),
        seen,
    )


def served(request: httpx.Request) -> httpx.Response:
    digest = hashlib.sha256(DATA).hexdigest()
    return httpx.Response(200, content=DATA, headers={"ETag": f'"{digest}"'})


@pytest.mark.parametrize(
    "media_id", ["../admin", "a/b", "..", ".", "%2e%2e", "a b", "x?y=1", "x#f", "a\b", "é"]
)
async def test_a_media_id_can_only_ever_be_one_plain_path_segment(media_id: str) -> None:
    f, seen = guarded(served)
    with pytest.raises(MediaUnavailableError, match="not acceptable"):
        await f.fetch("t", ref(media_id), max_bytes=1000)
    assert seen == []  # the payload's id never reached the network


async def test_the_address_is_resolved_once_and_pinned_with_the_original_host_header() -> None:
    f, seen = guarded(served, resolved=["93.184.216.34"])
    assert await f.fetch("t", ref(), max_bytes=1000) == DATA
    (request,) = seen
    assert request.url.host == "93.184.216.34"  # the vetted address, not a second DNS lookup
    assert request.headers["host"] == "gateway.example"
    assert request.url.path == "/api/v1/media/med_1/content"


async def test_a_gateway_that_resolves_to_a_private_address_is_refused_unless_allowed() -> None:
    f, seen = guarded(served, resolved=["10.0.0.7"])
    with pytest.raises(MediaUnavailableError, match="DESTINATION_NOT_ALLOWED"):
        await f.fetch("t", ref(), max_bytes=1000)
    assert seen == []
    allowed, _ = guarded(served, resolved=["10.0.0.7"], private_ok=True)  # the operator's choice
    assert await allowed.fetch("t", ref(), max_bytes=1000) == DATA


async def test_plain_http_is_refused_when_tls_is_required() -> None:
    insecure = ResolvedConnection(connection_id="relayplane", base_url="http://gateway.example")
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return served(request)

    strict = RelayPlaneMediaFetcher(
        StaticConnectionResolver({"relayplane": insecure}),
        client=httpx.AsyncClient(transport=httpx.MockTransport(record)),
    )  # tls_required is the default
    with pytest.raises(MediaUnavailableError, match="INSECURE_CONNECTION"):
        await strict.fetch("t", ref(), max_bytes=1000)
    assert seen == []


async def test_a_redirect_is_never_followed() -> None:
    f, seen = guarded(
        lambda request: httpx.Response(302, headers={"location": "http://169.254.169.254/latest"})
    )
    with pytest.raises(MediaUnavailableError, match="302"):
        await f.fetch("t", ref(), max_bytes=1000)
    assert len(seen) == 1  # one request, to the gateway, and no second one to wherever it pointed
