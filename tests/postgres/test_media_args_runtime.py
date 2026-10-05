"""Phase 15b on the durable runtime: a file the contact sent is attached to a ticket, end to end.

The contact sends a picture, asks for it to be attached, confirms, and the external API receives who
the file is (and, when the tool asks for it, the file's content). The handle lives in the
conversation's own state (so it survives restarts and goes with the conversation).
"""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest

from conversation_agent.adapters.clock import SystemClock
from conversation_agent.adapters.llm.fake import FakeLLM, text_response, tool_call_response
from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.adapters.senders.console import ConsoleChannel
from conversation_agent.adapters.tools.http import HTTPToolProvider, local_dev_connection
from conversation_agent.app.runtime import Runtime
from conversation_agent.core.models.media import MediaReference
from support.builders import IDENTITY
from support.media_agent import CONNECTION, compiled_media_agent

CLOCK = SystemClock("America/Sao_Paulo")
SHA = "d" * 64
PICTURE = MediaReference(
    media_id="m-print",
    kind="image",
    mime_type="image/jpeg",
    size_bytes=11,
    sha256=SHA,
    filename="print.jpg",
)


class Fetcher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    async def fetch(self, tenant_id: str, media: MediaReference, *, max_bytes: int) -> bytes:
        self.calls.append((tenant_id, media.media_id, max_bytes))
        return b"jpeg-bytes!"


async def resolver(host: str, port: int) -> list[str]:
    return ["127.0.0.1"]


def deployment(
    db: PostgresDatabase,
    llm: FakeLLM,
    seen: list[httpx.Request],
    *,
    content: bool,
    fetcher: Fetcher,
) -> tuple[Runtime, ConsoleChannel, list[str]]:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"id": "att-9"})

    provider = HTTPToolProvider.static(
        {CONNECTION: local_dev_connection("http://tickets.local", CONNECTION)},
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        resolve_host=resolver,  # no DNS in a test: the name points at a local address
        media_fetcher=fetcher,
    )
    out: list[str] = []
    channel = ConsoleChannel(IDENTITY, CLOCK, write=out.append)
    runtime = Runtime.build(
        db=db,
        compiled=compiled_media_agent(content=content),
        llm=llm,
        providers={"http": provider},
        sender=channel,
        clock=CLOCK,
    )
    return runtime, channel, out


def attach_llm() -> FakeLLM:
    return FakeLLM(
        [
            tool_call_response("files__attach", {"ticket": "77", "file": "media_1"}),
            text_response("Posso anexar o print."),
        ]
    )


async def attach_flow(
    runtime: Runtime, channel: ConsoleChannel
) -> None:  # the contact sends the file, then asks; the bot proposes; the contact confirms
    first = channel.inbound("anexa isso ao chamado 77").model_copy(update={"media": (PICTURE,)})
    await runtime.receive(first)
    await runtime.drain()
    await runtime.receive(channel.inbound("sim"))
    await runtime.drain()


async def test_a_file_is_attached_after_the_contact_confirms_and_the_api_learns_who_it_is(
    db: PostgresDatabase,
) -> None:
    seen: list[httpx.Request] = []
    runtime, channel, out = deployment(db, attach_llm(), seen, content=False, fetcher=Fetcher())
    await attach_flow(runtime, channel)

    assert "Anexar print.jpg ao chamado 77" in out[0]  # the question names the file
    (request,) = seen
    assert request.method == "POST" and request.url.path == "/tickets/77/files"
    body = json.loads(request.content)
    assert body == {  # the reference, the checksum, NO content: this tool did not ask for bytes
        "file": {
            "handle": "media_1",
            "media_id": "m-print",
            "kind": "image",
            "mime_type": "image/jpeg",
            "size_bytes": 11,
            "sha256": SHA,
            "filename": "print.jpg",
        }
    }
    assert out[-1] == "bot> Anexei print.jpg ao chamado 77 (anexo att-9)."


async def test_a_tool_that_asks_for_the_content_gets_it_checked_and_bounded(
    db: PostgresDatabase,
) -> None:
    seen: list[httpx.Request] = []
    fetcher = Fetcher()
    runtime, channel, _ = deployment(db, attach_llm(), seen, content=True, fetcher=fetcher)
    await attach_flow(runtime, channel)

    (request,) = seen
    file = json.loads(request.content)["file"]
    assert base64.b64decode(file["content_base64"]) == b"jpeg-bytes!"
    assert file["sha256"] == SHA and file["media_id"] == "m-print"
    ((tenant, media_id, limit),) = fetcher.calls
    assert (tenant, media_id) == (IDENTITY.tenant_id, "m-print")
    assert 0 < limit < 256 * 1024  # bounded by what the connection lets a request carry


async def test_a_file_that_cannot_be_retrieved_is_a_failure_before_anything_is_sent(
    db: PostgresDatabase,
) -> None:
    class Gone:
        async def fetch(self, tenant_id: str, media: MediaReference, *, max_bytes: int) -> bytes:
            raise RuntimeError("expired at the gateway")

    seen: list[httpx.Request] = []
    runtime, channel, out = deployment(
        db,
        FakeLLM([*attach_llm()._script, text_response("Não consegui anexar.")]),
        seen,
        content=True,
        fetcher=Gone(),  # type: ignore[arg-type]
    )
    await attach_flow(runtime, channel)
    assert seen == []  # the ticket system never heard of it
    assert out[-1] == "bot> Não consegui anexar."


async def test_the_files_of_a_conversation_survive_a_restart_with_the_conversation_state(
    db: PostgresDatabase,
) -> None:
    seen: list[httpx.Request] = []
    runtime, channel, _ = deployment(
        db, FakeLLM([text_response("Recebi o print.")]), seen, content=False, fetcher=Fetcher()
    )
    first = channel.inbound("olha").model_copy(update={"media": (PICTURE,)})
    await runtime.receive(first)
    await runtime.drain()

    state = await db.pool.fetchval("SELECT state_json FROM conversation_states")
    assert [m["handle"] for m in state["media"]] == ["media_1"] and state["media_seq"] == 1
    assert "content_base64" not in json.dumps(state)  # references only, never the file
    assert state["media"][0]["sha256"] == SHA

    # a NEW process (new runtime) picks the conversation up and can still use the handle
    seen2: list[httpx.Request] = []
    runtime2, channel2, out2 = deployment(
        db,
        FakeLLM(
            [
                tool_call_response("files__attach", {"ticket": "5", "file": "media_1"}),
                text_response("Posso anexar o print."),
            ]
        ),
        seen2,
        content=False,
        fetcher=Fetcher(),
    )
    await runtime2.receive(channel2.inbound("anexa aquele ao chamado 5"))
    await runtime2.drain()
    assert "Anexar print.jpg ao chamado 5" in out2[-1]


@pytest.mark.parametrize("handle", ["media_2", "media_0", "other"])
async def test_a_handle_the_conversation_does_not_have_proposes_nothing(
    db: PostgresDatabase, handle: str
) -> None:
    seen: list[httpx.Request] = []
    llm = FakeLLM(
        [
            tool_call_response("files__attach", {"ticket": "77", "file": handle}),
            text_response("Não achei esse arquivo."),
        ]
    )
    runtime, channel, out = deployment(db, llm, seen, content=False, fetcher=Fetcher())
    first = channel.inbound("anexa").model_copy(update={"media": (PICTURE,)})
    await runtime.receive(first)
    await runtime.drain()
    assert await db.pool.fetchval("SELECT count(*) FROM pending_actions") == 0
    assert seen == [] and "SIM" not in out[-1]


def test_an_agent_without_a_file_input_keeps_the_digest_it_always_had() -> None:
    from conversation_agent.adapters.manifest.yaml_loader import load_manifest_file
    from conversation_agent.core.compiler import agent_document, compile_manifest
    from vertical_slice.wiring import MANIFEST_PATH

    document: dict[str, Any] = agent_document(
        compile_manifest(load_manifest_file(MANIFEST_PATH)).agent
    )
    for tool in document["tools"]:
        assert "media_content" not in (tool.get("http") or {})


async def test_erasing_the_contact_takes_the_handles_with_the_conversation(
    db: PostgresDatabase,
) -> None:  # a file's handle lives only in the conversation state: retention and erasure reach it
    from conversation_agent.adapters.postgres.retention import RetentionService

    seen: list[httpx.Request] = []
    runtime, channel, _ = deployment(
        db, FakeLLM([text_response("Recebi.")]), seen, content=False, fetcher=Fetcher()
    )
    first = channel.inbound("olha").model_copy(update={"media": (PICTURE,)})
    await runtime.receive(first)
    await runtime.drain()
    assert (
        await db.pool.fetchval(
            "SELECT jsonb_array_length(state_json->'media') FROM conversation_states"
        )
        == 1
    )

    result = await RetentionService(db).erase_contact(
        IDENTITY.tenant_id, IDENTITY.contact_id, actor="dpo", reason="art. 18"
    )
    assert result.status == "erased"
    assert await db.pool.fetchval("SELECT count(*) FROM conversation_states") == 0
