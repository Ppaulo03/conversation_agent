"""`python -m conversation_agent.app.compile agent.yaml`: compile a manifest and say what is wrong.

Exit code 0 = compiled (prints agent, version and digest), 1 = diagnostics, 2 = unreadable file.
Meant for CI: an agent that does not compile never reaches a registry.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

from conversation_agent.adapters.manifest.yaml_loader import load_manifest_file
from conversation_agent.core.compiler import CompileError, compile_manifest
from conversation_agent.core.errors import DefinitionError


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: python -m conversation_agent.app.compile <manifest.yaml>", file=sys.stderr)
        return 2
    try:
        raw = load_manifest_file(args[0])
    except (OSError, DefinitionError, UnicodeDecodeError) as exc:
        print(f"cannot read {args[0]}: {exc}", file=sys.stderr)
        return 2
    try:
        compiled = compile_manifest(raw)
    except CompileError as exc:
        print(f"{args[0]}: {len(exc.diagnostics)} error(s)", file=sys.stderr)
        for diagnostic in exc.diagnostics:
            print(f"  {diagnostic}", file=sys.stderr)
        return 1
    print(f"OK {compiled.agent_id} {compiled.version} digest={compiled.digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
