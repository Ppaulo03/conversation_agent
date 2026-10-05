"""Phase 15b: files the contact sent are addressable by handle and can be passed to a tool.

The model only ever names a handle (`media_2`); the runtime resolves it against THIS conversation's
files (INV-068). The tool receives who the file is (and the confirmation covers that file), never
something the model wrote.
"""

from __future__ import annotations

from typing import Any

import pytest

from conversation_agent.adapters.llm.fake import FakeLLM, text_response, tool_call_response
from conversation_agent.adapters.tools.fake import FakeToolProvider
from conversation_agent.app.wiring import build_memory_engine
from conversation_agent.core.definitions.media_args import llm_input_schema, media_fields
from conversation_agent.core.models.conversation import ConversationState
from conversation_agent.core.models.media import (
    MAX_MEDIA_HANDLES,
    MediaReference,
    register_media,
)
from support.builders import IDENTITY, new_clock
from support.media_agent import compiled_media_agent

SHA_A = "a" * 64
SHA_B = "b" * 64


def image(media_id: str, sha: str | None = None, name: str | None = None) -> MediaReference:
    return MediaReference(
        media_id=media_id,
        kind="image",
        mime_type="image/jpeg",
        size_bytes=2048,
        sha256=sha or ("c" * 64),
        filename=name,
    )


# --- the registry (pure) ---


def test_each_ready_file_gets_a_handle_and_a_redelivery_keeps_it() -> None:
    kept, seq, handles = register_media((), 0, (image("m-a"), image("m-b")))
    assert handles == {"m-a": "media_1", "m-b": "media_2"} and seq == 2
    again, seq2, handles2 = register_media(kept, seq, (image("m-a"),))  # delivered twice
    assert handles2 == {"m-a": "media_1"} and seq2 == 2 and again == kept


def test_a_file_the_channel_could_not_deliver_gets_no_handle() -> None:
    rejected = MediaReference(
        media_id="x", kind="image", mime_type="image/png", status="rejected", reason="too_large"
    )
    kept, seq, handles = register_media((), 0, (rejected,))
    assert kept == () and seq == 0 and handles == {}


def test_a_conversation_keeps_the_newest_ten_and_never_reuses_a_handle() -> None:
    kept, seq = (), 0
    for i in range(1, MAX_MEDIA_HANDLES + 3):  # 12 files
        kept, seq, _ = register_media(kept, seq, (image(f"m-{i}"),))
    assert [m.handle for m in kept] == [f"media_{i}" for i in range(3, 13)]  # 1 and 2 are gone
    assert len(kept) == MAX_MEDIA_HANDLES and seq == 12
    again, seq2, handles = register_media(kept, seq, (image("m-1"),))  # the evicted file returns
    assert handles == {"m-1": "media_13"} and seq2 == 13  # a NEW handle: media_1 is never reused
    assert again[-1].media_id == "m-1"


# --- what the model sees ---


def test_the_model_is_shown_a_handle_never_the_file_object() -> None:
    capability = compiled_media_agent().agent.capabilities[0]
    assert media_fields(capability.input_model) == ("file",)
    schema = llm_input_schema(capability.input_model)
    file_schema = schema["properties"]["file"]
    assert file_schema["type"] == "string" and file_schema["pattern"] == r"^media_[0-9]{1,9}$"
    assert (
        "media_1" in file_schema["description"]
        and "The file to attach" in file_schema["description"]
    )
    assert "MediaArg" not in str(schema) and "sha256" not in str(schema)


# --- a turn ---


def engine_for(llm: FakeLLM) -> Any:
    return build_memory_engine(
        compiled_media_agent(),
        llm,
        providers={"http": FakeToolProvider({})},
        clock=new_clock(),
    )


async def turn(
    engine: Any,
    state: ConversationState,
    text: str,
    media: tuple[MediaReference, ...] = (),
    n: int = 1,
) -> Any:
    return await engine.process_turn(IDENTITY, state, text, turn_id=f"t{n}", media=media)


def seen_by_the_model(llm: FakeLLM) -> str:
    request = llm.requests[0]
    return "".join(p.text for p in request.messages[-1].parts if hasattr(p, "text"))


async def test_an_agent_with_a_file_capability_names_the_handle_in_the_message() -> None:
    llm = FakeLLM([text_response("Vi o arquivo.")])
    await turn(engine_for(llm), ConversationState(), "segue o print", (image("m-a"),))
    assert seen_by_the_model(llm) == "segue o print\n[image received] [media_1]"


async def test_an_agent_without_one_sees_exactly_what_it_saw_before() -> None:
    from conversation_agent.adapters.manifest.yaml_loader import load_manifest_file
    from conversation_agent.core.compiler import compile_manifest
    from vertical_slice.wiring import MANIFEST_PATH

    llm = FakeLLM([text_response("ok")])
    plain = compile_manifest(load_manifest_file(MANIFEST_PATH))
    engine = build_memory_engine(plain, llm, providers={"http": FakeToolProvider({})})
    await turn(engine, ConversationState(), "segue", (image("m-a"),))
    assert seen_by_the_model(llm) == "segue\n[image received]"  # no handle: it cannot use one


async def test_the_model_attaches_a_file_by_handle_and_the_proposal_covers_that_file() -> None:
    llm = FakeLLM(
        [
            tool_call_response("files__attach", {"ticket": "77", "file": "media_1"}),
            text_response("Posso anexar o print."),
        ]
    )
    outcome = await turn(
        engine_for(llm),
        ConversationState(),
        "anexa ao chamado 77",
        (image("m-a", SHA_A, "print.jpg"),),
    )
    (proposal,) = outcome.proposed
    request = proposal.request
    assert request.args["file"] == {  # who the file is: resolved by the runtime, checksum included
        "handle": "media_1",
        "media_id": "m-a",
        "kind": "image",
        "mime_type": "image/jpeg",
        "size_bytes": 2048,
        "sha256": SHA_A,
        "filename": "print.jpg",
    }
    assert request.summary == "Anexar print.jpg ao chamado 77"  # the question names the file


async def test_two_different_files_are_two_different_confirmations() -> None:
    hashes = []
    for sha in (SHA_A, SHA_B):
        llm = FakeLLM(
            [
                tool_call_response("files__attach", {"ticket": "77", "file": "media_1"}),
                text_response("ok"),
            ]
        )
        outcome = await turn(
            engine_for(llm), ConversationState(), "anexa", (image("m-a", sha, "print.jpg"),)
        )
        hashes.append(outcome.proposed[0].request.args_hash)
    assert hashes[0] != hashes[1]  # a "yes" for one file can never authorise another (INV-010)


async def test_a_handle_from_an_earlier_message_still_works_later() -> None:
    llm1 = FakeLLM([text_response("Recebi.")])
    first = await turn(engine_for(llm1), ConversationState(), "olha isso", (image("m-a", SHA_A),))
    assert [m.handle for m in first.state.media] == ["media_1"]  # kept with the conversation

    llm2 = FakeLLM(
        [
            tool_call_response("files__attach", {"ticket": "9", "file": "media_1"}),
            text_response("ok"),
        ]
    )
    second = await turn(engine_for(llm2), first.state, "anexa aquele ao chamado 9", n=2)
    assert second.proposed[0].request.args["file"]["sha256"] == SHA_A


@pytest.mark.parametrize(
    "bad",
    [
        "media_9",  # a handle this conversation does not have
        "m-a",  # the channel's own id: the model does not get to name it
        "../../etc/passwd",
        {
            "handle": "media_1",
            "media_id": "other-conversations-file",
            "kind": "image",
            "mime_type": "image/png",
        },
        42,
    ],
)
async def test_the_model_cannot_name_a_file_the_conversation_does_not_have(bad: object) -> None:
    llm = FakeLLM(
        [
            tool_call_response("files__attach", {"ticket": "77", "file": bad}),
            text_response("não deu"),
        ]
    )
    outcome = await turn(engine_for(llm), ConversationState(), "anexa", (image("m-a", SHA_A),))
    assert outcome.proposed == ()  # nothing was proposed, so nothing can be confirmed
    result = "".join(
        str(part.content)
        for message in llm.requests[1].messages
        for part in message.parts
        if hasattr(part, "content")
    )
    assert "must be the handle of a file the contact sent" in result
    assert "available: media_1" in result  # it is told what it has, and only that


async def test_a_file_that_aged_out_cannot_be_attached_any_more() -> None:
    state = ConversationState()
    for i in range(1, MAX_MEDIA_HANDLES + 2):  # 11 files: media_1 is dropped
        llm = FakeLLM([text_response("ok")])
        outcome = await turn(engine_for(llm), state, f"arquivo {i}", (image(f"m-{i}", SHA_A),), n=i)
        state = outcome.state
    assert "media_1" not in {m.handle for m in state.media}

    llm = FakeLLM(
        [
            tool_call_response("files__attach", {"ticket": "1", "file": "media_1"}),
            text_response("x"),
        ]
    )
    gone = await turn(engine_for(llm), state, "anexa o primeiro", n=20)
    assert gone.proposed == ()
    llm = FakeLLM(
        [
            tool_call_response("files__attach", {"ticket": "1", "file": "media_11"}),
            text_response("x"),
        ]
    )
    recent = await turn(engine_for(llm), state, "anexa o último", n=21)
    assert len(recent.proposed) == 1


# --- definitions ---


def test_a_file_argument_must_travel_in_an_http_body() -> None:
    from conversation_agent.core.compiler import CompileError, compile_manifest
    from support.media_agent import manifest

    raw = manifest()
    raw["tools"][0]["http"]["body"] = []  # the file would vanish (or end up elsewhere)
    with pytest.raises(CompileError, match="MEDIA_NOT_IN_BODY"):
        compile_manifest(raw)


def test_a_literal_value_for_a_file_field_is_refused() -> None:
    from conversation_agent.core.definitions.schema_spec import FieldSpec

    with pytest.raises(ValueError, match="takes no literal value"):
        FieldSpec(type="media", required=False, default="media_1")


def test_content_can_only_be_asked_for_fields_of_the_body() -> None:
    from conversation_agent.core.definitions.tool import HTTPRequestSpec

    with pytest.raises(ValueError, match="media_content"):
        HTTPRequestSpec(method="POST", path="/x", body=("a",), media_content=("b",))
