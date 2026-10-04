"""Architecture tests: INV-001, INV-003 (structural half), INV-008."""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "conversation_agent"

INNER_LAYERS = ("core", "ports", "tools", "engine")
ADAPTER_ONLY_SDKS = {
    "httpx", "anthropic", "openai", "psycopg", "psycopg2", "asyncpg", "sqlalchemy",
    "fastapi", "uvicorn", "starlette", "mcp", "relayplane", "requests", "aiohttp",
}  # fmt: skip


def imported_modules(source: str) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module)
    return modules


def violations(layer_dir: Path) -> list[str]:
    found: list[str] = []
    for path in layer_dir.rglob("*.py"):
        for module in imported_modules(path.read_text(encoding="utf-8")):
            top = module.split(".")[0]
            if module.startswith(("conversation_agent.adapters", "conversation_agent.app")):
                found.append(f"{path.relative_to(ROOT)} imports {module}")
            elif top in ADAPTER_ONLY_SDKS:
                found.append(f"{path.relative_to(ROOT)} imports SDK {module}")
    return found


def test_architecture_core_engine_do_not_import_adapters() -> None:  # INV-001
    problems = [v for layer in INNER_LAYERS for v in violations(SRC / layer)]
    assert problems == []


def test_scanner_detects_adapter_and_sdk_imports() -> None:
    """Guards the guard: the scanner above must actually catch violations."""
    mods = imported_modules(
        "import httpx\nfrom conversation_agent.adapters.llm.fake import FakeLLM\nimport json\n"
    )
    assert "httpx" in mods
    assert any(m.startswith("conversation_agent.adapters") for m in mods)


def test_import_linter_contracts_pass() -> None:  # INV-001 (enforced by import-linter too)
    result = subprocess.run(
        [sys.executable, "-c", "from importlinter.cli import lint_imports_command as m; m()"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_adapters_do_not_import_engine() -> None:
    for path in (SRC / "adapters").rglob("*.py"):
        for module in imported_modules(path.read_text(encoding="utf-8")):
            assert not module.startswith(("conversation_agent.engine", "conversation_agent.app"))


def test_only_tool_runner_executes_tool_providers() -> None:
    """INV-003 (structural): no engine module other than ToolRunner calls a provider."""
    for path in (SRC / "engine").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if path.name == "tool_runner.py":
            assert "provider.execute(" in text
        else:
            assert ".execute(" not in text.replace("self._pipeline.execute(", "")


@pytest.mark.parametrize("path", sorted(SRC.rglob("*.py")), ids=lambda p: str(p.relative_to(SRC)))
def test_framework_never_imports_examples_or_external_systems(path: Path) -> None:  # INV-008
    for module in imported_modules(path.read_text(encoding="utf-8")):
        assert module.split(".")[0] not in {"scheduling_api", "vertical_slice"}


def test_runtime_does_not_own_business_state() -> None:  # INV-008
    """The framework source carries no business-domain vocabulary and its state models only
    hold conversational data."""
    domain_words = re.compile(
        r"\b(scheduling|booking|reservation|appointment|ticket|faq|chamado)s?\b", re.IGNORECASE
    )
    offenders = [
        str(p.relative_to(ROOT))
        for p in SRC.rglob("*.py")
        if domain_words.search(p.read_text(encoding="utf-8"))
    ]
    assert offenders == []

    from conversation_agent.core.models.conversation import ConversationState

    assert set(ConversationState.model_fields) == {"history", "proposals", "flows"}


def test_outbound_delivery_only_from_outbox() -> None:  # INV-007 (structural half)
    """Only the OutboxWorker may depend on the MessageSender port; nothing else in the
    framework can put bytes on a channel."""
    users = sorted(
        p.relative_to(SRC).as_posix()
        for p in SRC.rglob("*.py")
        if "conversation_agent.ports.sender" in imported_modules(p.read_text(encoding="utf-8"))
        and p.parent.name != "adapters"
        and "adapters" not in p.parts
    )
    assert users == ["engine/outbox_worker.py"]
