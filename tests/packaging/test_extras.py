"""Phase 13: the base install is light. `asyncpg` (extra `postgres`) and `anthropic` (extra
`anthropic`) and OpenTelemetry are only needed by the code that talks to them; everything else
imports without."""

from __future__ import annotations

import subprocess
import sys
import textwrap
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Importable with NEITHER optional dependency installed.
LIGHT = [
    "conversation_agent.core.compiler",
    "conversation_agent.engine.turn_engine",
    "conversation_agent.engine.turn_coordinator",
    "conversation_agent.adapters.llm.fake",
    "conversation_agent.adapters.llm.openai_compat",
    "conversation_agent.adapters.tools.http",
    "conversation_agent.adapters.senders.console",
    "conversation_agent.adapters.transcribers.openai_compat",  # speech-to-text over plain httpx
    "conversation_agent.app.stt_factory",
    "conversation_agent.adapters.manifest.yaml_loader",
    "conversation_agent.app.llm_factory",
    "conversation_agent.app.compile",
    "conversation_agent.app.evals",
    "conversation_agent.app.wiring",
    "conversation_agent.app.ops",  # `ops check` runs in CI, without a database
]
# These need the postgres extra and must SAY so instead of failing with a bare ModuleNotFoundError.
NEEDS_POSTGRES = [
    "conversation_agent.adapters.postgres.db",
    "conversation_agent.app.runtime",
    "conversation_agent.app.serve",
]

BLOCK = """
import importlib.abc, sys
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in {"asyncpg", "anthropic", "opentelemetry"}:
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
sys.meta_path.insert(0, Block())
"""


def run(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", BLOCK + textwrap.dedent(code)],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )


def test_the_light_modules_import_without_the_optional_dependencies() -> None:
    result = run("import importlib\n" + "\n".join(f"importlib.import_module({m!r})" for m in LIGHT))
    assert result.returncode == 0, result.stderr[-1500:]


def test_the_postgres_code_says_which_extra_it_needs() -> None:
    for module in NEEDS_POSTGRES:
        result = run(f"import {module}")
        assert result.returncode != 0
        assert "conversation-agent[postgres]" in result.stderr, (module, result.stderr[-600:])


def test_choosing_anthropic_without_its_sdk_says_which_extra_it_needs() -> None:
    result = run(
        """
        from conversation_agent.app.llm_factory import LLMConfig, build_llm
        build_llm(LLMConfig(provider="anthropic", api_key="k", model="m"))
        """
    )
    assert result.returncode != 0 and "conversation-agent[anthropic]" in result.stderr


def test_choosing_opentelemetry_without_its_sdk_says_which_extra_it_needs() -> None:
    result = run("import conversation_agent.adapters.observability.opentelemetry")
    assert result.returncode != 0 and "conversation-agent[otel]" in result.stderr


def test_the_extras_are_declared_and_the_base_install_is_light() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    base = " ".join(project["dependencies"])
    assert "asyncpg" not in base and "anthropic" not in base and "opentelemetry" not in base
    extras = project["optional-dependencies"]
    assert any("asyncpg" in d for d in extras["postgres"])
    assert any("anthropic" in d for d in extras["anthropic"])
    assert any("opentelemetry-sdk" in d for d in extras["otel"])
    assert set(extras["all"]) >= {
        "conversation-agent[postgres]",
        "conversation-agent[anthropic]",
        "conversation-agent[otel]",
    }
