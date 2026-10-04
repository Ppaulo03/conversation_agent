"""`python -m conversation_agent.app.ops render|check [--slo ops/slo.yaml] [--out ops]`.

`render` writes the Prometheus alert rules and the Grafana dashboard generated from the SLO file;
`check` exits 1 when the SLOs reference an unknown metric or runbook anchor, or when the generated
files on disk are stale. Run `check` in CI.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from conversation_agent.adapters.observability.slo import (
    SLOFile,
    load,
    problems,
    render_dashboard,
    render_rules,
)

USAGE = "usage: python -m conversation_agent.app.ops render|check [--slo FILE] [--out DIR]"
RULES_FILE = "alerts.rules.yml"
DASHBOARD_FILE = "grafana-dashboard.json"
RUNBOOK = Path("docs") / "OPERATIONS.md"


def _option(args: list[str], name: str, default: str) -> str:
    if name not in args:
        return default
    at = args.index(name)
    value = args[at + 1] if at + 1 < len(args) else ""
    del args[at : at + 2]
    return value or default


def generated(slos: SLOFile) -> dict[str, str]:
    return {RULES_FILE: render_rules(slos), DASHBOARD_FILE: render_dashboard(slos)}


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    slo_path = Path(_option(args, "--slo", "ops/slo.yaml"))
    out = Path(_option(args, "--out", "ops"))
    if len(args) != 1 or args[0] not in ("render", "check"):
        print(USAGE, file=sys.stderr)
        return 2
    try:
        slos = load(slo_path.read_text(encoding="utf-8"))
        runbook = RUNBOOK.read_text(encoding="utf-8") if RUNBOOK.exists() else None
    except (OSError, ValidationError, ValueError) as exc:
        print(f"cannot read the SLO file: {exc}", file=sys.stderr)
        return 2
    found = problems(slos, runbook)
    if found:
        for problem in found:
            print(f"  {problem}", file=sys.stderr)
        return 1
    files = generated(slos)
    if args[0] == "render":
        out.mkdir(parents=True, exist_ok=True)
        for name, content in files.items():
            (out / name).write_text(content, encoding="utf-8", newline="\n")
        print(f"wrote {', '.join(files)} to {out}")
        return 0
    stale = [
        n
        for n, c in files.items()
        if not (out / n).exists() or (out / n).read_text(encoding="utf-8") != c
    ]
    if stale:
        print(f"stale (run `render`): {', '.join(stale)}", file=sys.stderr)
        return 1
    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
