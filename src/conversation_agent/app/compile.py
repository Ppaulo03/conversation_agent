"""`python -m conversation_agent.app.compile agent.yaml [--packs DIR]`: compile a manifest and say
what is wrong. `--packs` is the directory the manifest's `packs` are installed from.

Exit code 0 = compiled (prints agent, version and digest), 1 = diagnostics, 2 = unreadable file.
Meant for CI: an agent that does not compile never reaches a registry.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

from conversation_agent.adapters.manifest.pack_loader import DirectoryPackLoader
from conversation_agent.adapters.manifest.yaml_loader import load_manifest_file
from conversation_agent.core.compiler import CompileError, compile_manifest
from conversation_agent.core.errors import DefinitionError

USAGE = "usage: python -m conversation_agent.app.compile <manifest.yaml> [--packs DIR]"


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    packs_dir: str | None = None
    if "--packs" in args:
        at = args.index("--packs")
        if at + 1 >= len(args):
            print("--packs needs a directory", file=sys.stderr)
            return 2
        packs_dir = args[at + 1]
        del args[at : at + 2]
    if len(args) != 1:
        print(USAGE, file=sys.stderr)
        return 2
    try:
        raw = load_manifest_file(args[0])
    except (OSError, DefinitionError, UnicodeDecodeError) as exc:
        print(f"cannot read {args[0]}: {exc}", file=sys.stderr)
        return 2
    catalog = None
    if packs_dir is not None:
        declared = raw.get("packs")
        catalog = DirectoryPackLoader(packs_dir).catalog_for(
            declared if isinstance(declared, list) else []
        )
    try:
        compiled = compile_manifest(raw, catalog)
    except CompileError as exc:
        print(f"{args[0]}: {len(exc.diagnostics)} error(s)", file=sys.stderr)
        for diagnostic in exc.diagnostics:
            print(f"  {diagnostic}", file=sys.stderr)
        return 1
    print(f"OK {compiled.agent_id} {compiled.version} digest={compiled.digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
