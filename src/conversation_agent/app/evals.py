"""`python -m conversation_agent.app.evals agent.yaml suite.yaml [--packs DIR] [--cassette FILE]
[--json OUT]`: run an eval suite against an agent, offline.

Tools are served from a recorded cassette (`ReplayToolProvider`): an unrecorded call is an explicit
miss, never an invented answer; without a cassette every tool call misses, which is exactly what a
suite of Flow-only or proposal-only scenarios needs. Exit code 0 = every scenario passed,
1 = failures (or a compile error), 2 = unreadable input. Meant to gate a release in CI.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from pydantic import ValidationError

from conversation_agent.adapters.manifest.pack_loader import DirectoryPackLoader
from conversation_agent.adapters.manifest.yaml_loader import load_manifest_file
from conversation_agent.adapters.tools.replay import ReplayToolProvider, load_cassette
from conversation_agent.app.eval_harness import EvalHarness, SuiteReport
from conversation_agent.core.compiler import CompiledAgent, CompileError, compile_manifest
from conversation_agent.core.definitions.evals import EvalSuite
from conversation_agent.core.errors import DefinitionError
from conversation_agent.ports.tool_provider import ToolProvider

USAGE = (
    "usage: python -m conversation_agent.app.evals <agent.yaml> <suite.yaml> "
    "[--packs DIR] [--cassette FILE] [--json OUT]"
)


def _option(args: list[str], name: str) -> str | None:
    if name not in args:
        return None
    at = args.index(name)
    if at + 1 >= len(args):
        raise ValueError(f"{name} needs a value")
    value = args[at + 1]
    del args[at : at + 2]
    return value


def load_suite(path: str | Path) -> EvalSuite:
    try:
        return EvalSuite.model_validate(load_manifest_file(path))
    except ValidationError as exc:
        error = exc.errors()[0]
        where = ".".join(str(p) for p in error["loc"])
        raise DefinitionError(f"{path}: invalid suite: {where}: {error['msg']}") from exc


async def run_suite(
    compiled: CompiledAgent, suite: EvalSuite, cassette_path: str | None
) -> SuiteReport:
    cassette = load_cassette(Path(cassette_path)) if cassette_path else {}
    kinds = sorted({t.provider for t in compiled.agent.tools})

    def providers() -> Mapping[str, ToolProvider]:
        return {kind: ReplayToolProvider(cassette) for kind in kinds}  # fresh queues per scenario

    return await EvalHarness(compiled, providers).run(suite)


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        packs_dir = _option(args, "--packs")
        cassette = _option(args, "--cassette")
        json_out = _option(args, "--json")
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    if len(args) != 2:
        print(USAGE, file=sys.stderr)
        return 2
    try:
        raw = load_manifest_file(args[0])
        suite = load_suite(args[1])
        declared = raw.get("packs")
        catalog = (
            DirectoryPackLoader(packs_dir).catalog_for(
                declared if isinstance(declared, list) else []
            )
            if packs_dir
            else None
        )
        compiled = compile_manifest(raw, catalog)
    except CompileError as exc:
        print(f"{args[0]}: {len(exc.diagnostics)} compile error(s)", file=sys.stderr)
        for diagnostic in exc.diagnostics:
            print(f"  {diagnostic}", file=sys.stderr)
        return 1
    except (OSError, DefinitionError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(f"cannot read input: {exc}", file=sys.stderr)
        return 2
    report = asyncio.run(run_suite(compiled, suite, cassette))
    for result in report.results:
        print(f"{'PASS' if result.passed else 'FAIL'} {result.scenario}")
        for failure in result.failures:
            print(f"     {failure}")
    print(f"{report.passed_count}/{report.total} scenarios passed")
    if json_out:
        Path(json_out).write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
