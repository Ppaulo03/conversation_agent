"""Phase 13: the composition helpers shipped in the package work from an installed copy."""

from __future__ import annotations

from pathlib import Path

import pytest

from conversation_agent.adapters.llm.fake import FakeLLM, text_response
from conversation_agent.app.wiring import build_memory_engine, load_agent, local_http_provider
from conversation_agent.core.compiler import CompileError
from conversation_agent.core.models.conversation import ConversationIdentity, ConversationState

ROOT = Path(__file__).resolve().parents[2]
SUPPORT = str(ROOT / "examples" / "support-agent" / "agent.yaml")


async def test_a_manifest_is_loaded_and_run_in_memory_with_no_example_code() -> None:
    compiled = load_agent(SUPPORT)
    http = local_http_provider({})
    engine = build_memory_engine(
        compiled, FakeLLM([text_response("Olá!")]), providers={"http": http}
    )
    identity = ConversationIdentity(
        tenant_id="t", channel_id="c", conversation_id="v", session_id="s", contact_id="p"
    )
    outcome = await engine.process_turn(identity, ConversationState(), "oi", turn_id="t1")
    assert outcome.reply == "Olá!"
    await http.aclose()


def test_a_manifest_that_does_not_compile_reports_every_diagnostic(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("agent_id: x\nversion: '1'\npersona: p\nsurprise: 1\n", encoding="utf-8")
    with pytest.raises(CompileError) as caught:
        load_agent(str(bad))
    assert len(caught.value.diagnostics) > 1  # all of them, not just the first
