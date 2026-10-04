"""The eval harness (Phase 10): suites as data, offline and deterministic, with assertions on what
was said, proposed and EXECUTED, usable as a release gate."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from conversation_agent.adapters.manifest.yaml_loader import load_manifest_file
from conversation_agent.app.eval_harness import SuiteReport
from conversation_agent.app.evals import load_suite, main, run_suite
from conversation_agent.core.compiler import compile_manifest
from conversation_agent.core.definitions.evals import (
    EvalScenario,
    EvalSuite,
    EvalTurn,
    LLMStep,
)
from conversation_agent.core.errors import DefinitionError
from support.support_domain import SUPPORT_DIR

AGENT = str(SUPPORT_DIR / "agent.yaml")
SUITE = str(SUPPORT_DIR / "evals.yaml")
CASSETTE = str(SUPPORT_DIR / "evals.cassette.json")


def suite_of(*turns: dict[str, Any], name: str = "s") -> EvalSuite:
    return EvalSuite.model_validate(
        {"suite": name, "scenarios": [{"name": "one", "turns": list(turns)}]}
    )


async def run(suite: EvalSuite, manifest: dict[str, Any] | None = None) -> SuiteReport:
    compiled = compile_manifest(manifest or load_manifest_file(AGENT))
    return await run_suite(compiled, suite, CASSETTE)


# --- the shipped suite is the gate ---


async def test_the_shipped_suite_passes_and_its_report_names_what_was_judged() -> None:
    report = await run(load_suite(SUITE))
    assert report.passed and (report.passed_count, report.total) == (5, 5)
    as_dict = report.to_dict()
    assert as_dict["agent"]["id"] == "support-demo" and len(as_dict["agent"]["digest"]) == 64
    assert as_dict["suite_digest"] == load_suite(SUITE).digest  # the evidence a release gate takes


async def test_a_report_becomes_the_release_evidence_for_exactly_that_agent() -> None:
    from conversation_agent.core.releases import ReleaseError, validate_evidence

    report = await run(load_suite(SUITE))
    evidence = report.evidence()
    validate_evidence(evidence, agent_digest=report.agent_digest, required=True)
    with pytest.raises(ReleaseError, match="different agent"):
        validate_evidence(evidence, agent_digest="0" * 64, required=True)
    manifest = copy.deepcopy(load_manifest_file(AGENT))
    manifest["flows"][0]["triggers"] = ["nunca dispara"]
    failing = (await run(load_suite(SUITE), manifest)).evidence()
    with pytest.raises(ReleaseError, match="did not pass"):
        validate_evidence(failing, agent_digest=failing.agent_digest, required=True)


async def test_a_regression_in_the_agent_fails_the_suite() -> None:
    manifest = copy.deepcopy(load_manifest_file(AGENT))
    manifest["flows"][0]["triggers"] = ["nunca dispara"]  # the ticket flow is no longer reachable
    report = await run(load_suite(SUITE), manifest)
    failed = [r for r in report.results if not r.passed]
    assert not report.passed and [r.scenario for r in failed] == [
        "a-ticket-is-collected-then-only-proposed",
        "asking-for-a-person-is-a-handoff",
        "a-question-while-the-subject-is-awaited-is-answered-not-taken-as-the-subject",
    ]
    assert failed[0].failures  # the Flow no longer answers, so the turn does not go as scripted


# --- what a turn can assert ---


async def test_a_capability_that_must_never_run_is_reported_when_it_did() -> None:
    suite = suite_of(
        {
            "user": "horário?",
            "llm": [
                {
                    "tool_call": {
                        "name": "support__search_faq",
                        "arguments": {"question": "horário"},
                    }
                },
                {"text": "ok"},
            ],
            "forbidden_capabilities": ["support.search_faq"],
            "executes_nothing": True,
            "proposes": "support.open_ticket",
            "max_llm_calls": 1,
        }
    )
    (result,) = (await run(suite)).results
    text = " ".join(result.failures)
    assert "forbidden capability 'support.search_faq' was used" in text
    assert "expected nothing executed" in text and "expected to propose" in text
    assert "2 model calls, at most 1" in text


async def test_every_failed_expectation_is_reported_not_just_the_first() -> None:
    suite = suite_of(
        {
            "user": "Quero abrir um chamado",
            "reply_contains": ["NOPE"],
            "reply_not_contains": ["assunto"],
            "reply_matches": ["^zzz"],
            "flow": "none",
            "handoff": True,
            "proposes_nothing": True,
            "capture": {"x": "never-matches"},
        }
    )
    (result,) = (await run(suite)).results
    assert len(result.failures) >= 6


async def test_a_scripted_model_that_runs_out_is_a_failed_scenario_not_a_crash() -> None:
    (result,) = (await run(suite_of({"user": "Qual a garantia?"}))).results
    assert not result.passed and "the turn failed" in result.failures[0]


async def test_scenarios_are_isolated_from_each_other() -> None:
    steps = [
        {"user": "Quero abrir um chamado", "flow": "ticket"},
        {"user": "Não consigo entrar", "flow": "ticket", "reply_contains": ["prioridade"]},
    ]
    suite = EvalSuite.model_validate(
        {
            "suite": "iso",
            "scenarios": [
                {"name": "first", "turns": steps},
                {"name": "second", "turns": steps},  # starts from scratch, not mid-flow
            ],
        }
    )
    assert (await run(suite)).passed


async def test_variables_fill_placeholders_and_captures_carry_across_turns() -> None:
    suite = EvalSuite.model_validate(
        {
            "suite": "vars",
            "variables": {"word": "chamado"},
            "scenarios": [
                {
                    "name": "v",
                    "turns": [
                        {
                            "user": "Quero abrir um {word}",
                            "flow": "ticket",
                            "capture": {"ask": "(assunto)"},
                        },
                        {"user": "o {ask} é lentidão", "flow": "ticket"},
                    ],
                }
            ],
        }
    )
    assert (await run(suite)).passed


# --- the data model ---


@pytest.mark.parametrize(
    "turn",
    [
        {"user": "x", "proposes": "a.b", "proposes_nothing": True},
        {"user": "x", "executes": ["a.b"], "executes_nothing": True},
        {"user": "x", "proposes": "a.b", "forbidden_capabilities": ["a.b"]},
        {"user": "x", "reply_matches": ["("]},
        {"user": "x", "surprise": 1},
        {"user": "x", "max_llm_calls": -1},
    ],
)
def test_a_contradictory_or_unknown_turn_is_refused(turn: dict[str, Any]) -> None:
    with pytest.raises((ValidationError, ValueError)):
        EvalTurn.model_validate(turn)


def test_a_model_step_is_exactly_one_thing() -> None:
    LLMStep(text="hi")
    with pytest.raises(ValidationError):
        LLMStep()
    with pytest.raises(ValidationError):
        LLMStep(text="hi", structured={"kind": "answer"})


def test_a_suite_needs_scenarios_with_unique_names() -> None:
    turn = EvalTurn(user="x")
    with pytest.raises(ValidationError, match="unique"):
        EvalSuite(
            suite="s",
            scenarios=(
                EvalScenario(name="a", turns=(turn,)),
                EvalScenario(name="a", turns=(turn,)),
            ),
        )
    with pytest.raises(ValidationError):
        EvalSuite(suite="s", scenarios=())


# --- the CLI is a gate ---


def test_the_cli_passes_prints_the_verdict_and_writes_the_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "report.json"
    code = main([AGENT, SUITE, "--cassette", CASSETTE, "--json", str(out)])
    assert code == 0 and "5/5 scenarios passed" in capsys.readouterr().out
    assert json.loads(out.read_text(encoding="utf-8"))["passed"] is True


def test_the_cli_exits_one_on_a_failed_scenario_and_says_why(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        yaml.safe_dump(
            {
                "suite": "bad",
                "scenarios": [
                    {"name": "wrong", "turns": [{"user": "Quero abrir um chamado", "flow": "none"}]}
                ],
            }
        ),
        encoding="utf-8",
    )
    assert main([AGENT, str(bad)]) == 1
    assert "FAIL wrong" in capsys.readouterr().out


def test_the_cli_distinguishes_unreadable_input_from_a_compile_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([AGENT]) == 2 and main([AGENT, SUITE, "--cassette"]) == 2
    assert main([AGENT, str(tmp_path / "missing.yaml")]) == 2
    broken = tmp_path / "broken.yaml"
    broken.write_text("suite: s\nscenarios: []\n", encoding="utf-8")
    assert main([AGENT, str(broken)]) == 2  # an invalid suite
    capsys.readouterr()
    agent = copy.deepcopy(load_manifest_file(AGENT))
    agent["allowed_capabilities"] = ["support.ghost"]
    wrong = tmp_path / "agent.yaml"
    wrong.write_text(yaml.safe_dump(agent, allow_unicode=True), encoding="utf-8")
    assert main([str(wrong), SUITE]) == 1  # an agent that does not compile is not evaluated
    assert "compile error" in capsys.readouterr().err


def test_load_suite_names_the_problem() -> None:
    with pytest.raises(DefinitionError, match="invalid suite"):
        load_suite(
            Path(__file__).parent.parent.parent / "examples" / "support-agent" / "agent.yaml"
        )


async def test_the_report_says_how_many_model_calls_each_scenario_made_and_why() -> None:
    report = await run(load_suite(SUITE))
    usage = {u.scenario: u for u in report.usage}
    faq = usage["faq-answered-from-the-base"]
    assert faq.llm_calls == 2 and faq.purposes == {"agent": 2}  # tool use, then the answer
    assert usage["a-ticket-is-collected-then-only-proposed"].llm_calls == 0  # the Flow answers
    assert report.to_dict()["llm_usage"][0]["scenario"] == "faq-answered-from-the-base"
