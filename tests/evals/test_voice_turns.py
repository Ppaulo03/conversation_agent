"""A suite can say that the contact sent a voice message with a given transcript: no audio, no
provider, deterministic. What the agent does with it depends on its `transcription` setting."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from conversation_agent.adapters.manifest.yaml_loader import load_manifest_file
from conversation_agent.app.evals import run_suite
from conversation_agent.core.compiler import compile_manifest
from conversation_agent.core.definitions.evals import EvalSuite, EvalTurn
from support.support_domain import SUPPORT_DIR

AGENT = str(SUPPORT_DIR / "agent.yaml")


def suite(*turns: dict[str, Any]) -> EvalSuite:
    return EvalSuite.model_validate(
        {"suite": "voice", "scenarios": [{"name": "v", "turns": list(turns)}]}
    )


async def run(suite_: EvalSuite, transcription: str | None) -> Any:
    manifest = copy.deepcopy(load_manifest_file(AGENT))
    if transcription is not None:
        manifest["transcription"] = transcription
    return await run_suite(compile_manifest(manifest), suite_, None)


ASK = {"voice": "quero falar com um atendente", "handoff": True, "max_llm_calls": 0}


async def test_an_agent_that_transcribes_acts_on_what_was_said() -> None:
    report = await run(suite(ASK), "on")
    assert report.passed, report.results[0].failures  # the spoken request reached the rules


async def test_an_agent_that_does_not_transcribe_never_hears_it() -> None:
    report = await run(
        suite(
            {
                "voice": "quero falar com um atendente",
                "handoff": False,  # the words were never read
                "llm": [{"text": "Não consigo ouvir áudios, pode escrever?"}],
                "reply_contains": ["pode escrever"],
            }
        ),
        None,  # the default: off
    )
    assert report.passed, report.results[0].failures


async def test_a_failing_expectation_about_a_voice_turn_is_reported() -> None:
    report = await run(suite({**ASK, "handoff": False}), "on")
    assert not report.passed
    assert "expected handoff=False" in report.results[0].failures[0]


async def test_text_and_voice_can_arrive_together() -> None:
    report = await run(
        suite({"user": "oi", "voice": "quero falar com um atendente", "handoff": True}), "on"
    )
    assert report.passed, report.results[0].failures


def test_a_turn_needs_something_to_say() -> None:
    with pytest.raises(ValidationError, match="`user` message or a `voice`"):
        EvalTurn.model_validate({"reply_contains": ["x"]})
    assert EvalTurn.model_validate({"voice": "alô"}).user == ""


def test_a_suite_without_voice_turns_keeps_the_digest_it_always_had() -> None:
    import hashlib  # noqa: F401

    from conversation_agent.core.canonical import stable_hash

    plain = suite({"user": "oi"})
    document = plain.model_dump(mode="json")
    for scenario in document["scenarios"]:
        for turn in scenario["turns"]:
            turn.pop("voice")  # the shape of a suite written before voice turns existed
    assert plain.digest == stable_hash(document)
    assert suite({"voice": "alô"}).digest != suite({"user": "alô"}).digest
