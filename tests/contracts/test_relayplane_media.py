"""RelayPlaneMediaFetcher against the gateway simulator: claim-check bytes, bounded and verified."""

from __future__ import annotations

import hashlib

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
