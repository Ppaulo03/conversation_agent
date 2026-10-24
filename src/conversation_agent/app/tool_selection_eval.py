"""Offline evaluation of a tool selector against a labelled dataset (provider-agnostic).

    uv run python -m conversation_agent.app.tool_selection_eval \\
        --catalog examples/tool-selection-eval/catalog.json \\
        --dataset examples/tool-selection-eval/dataset.jsonl \\
        --provider rules --rules examples/tool-selection-eval/rules.yaml

`--provider` is `rules` or `package.module:factory` (a callable taking no arguments that returns a
`ToolSelector`), so a classifier or embeddings provider is measured with the same files.

Dataset (JSONL): `{"id": "m01", "message": "...", "expected": ["tool_a", "tool_b"]}`. `expected` are
the tools the model must be able to call for that message (empty: no tool needed). The catalog is
a JSON list of `{name, description, input_schema}`.

The trigger is bypassed (the selector always runs) so the numbers say how good the ranking is; in
production the filter only acts above the agent's limits. It uses the engine's own `decide`, so
fallbacks (provider error, no candidate) count exactly as they would at runtime.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from conversation_agent.adapters.tool_selection.rules import RulesToolSelector
from conversation_agent.core.definitions.tool_selection import ToolRule, ToolSelectionSettings
from conversation_agent.core.models.llm import LLMToolDefinition
from conversation_agent.engine.tool_selection import (
    SelectionMonitor,
    decide,
    estimate_tokens,
    reduce_catalog,
)
from conversation_agent.ports.tool_selector import ToolSelector


@dataclass(frozen=True)
class Example:
    id: str
    message: str
    expected: tuple[str, ...]


@dataclass
class Row:
    id: str
    message: str
    expected: list[str]
    shown: list[str]
    mode: str  # selected | full
    reason: str
    hit: bool  # every expected tool is among the ones shown
    missed: list[str] = field(default_factory=list)


@dataclass
class Report:
    examples: int
    hit_rate: float  # share of examples whose expected tools were all kept
    hit_rate_selected: float  # same, only where the filter actually reduced the list
    tool_recall: float  # expected tool occurrences kept / expected tool occurrences
    fallback_rate: float  # share of examples that got the full list
    tools_removed: float  # mean share of the catalog removed
    tokens_removed: float  # mean share of the (estimated) tool tokens removed
    catalog_tools: int
    catalog_tokens: int
    per_tool_recall: dict[str, float]
    rows: list[Row]


def load_catalog(path: Path) -> tuple[LLMToolDefinition, ...]:
    return tuple(LLMToolDefinition.model_validate(t) for t in json.loads(path.read_text("utf-8")))


def load_dataset(path: Path, catalog: tuple[LLMToolDefinition, ...]) -> list[Example]:
    names = {t.name for t in catalog}
    examples: list[Example] = []
    for n, line in enumerate(path.read_text("utf-8").splitlines(), 1):
        if not line.strip():
            continue
        raw = json.loads(line)
        unknown = set(raw["expected"]) - names
        if unknown:
            raise ValueError(f"{path}:{n}: expected tools not in the catalog: {sorted(unknown)}")
        examples.append(Example(raw["id"], raw["message"], tuple(raw["expected"])))
    if len({e.id for e in examples}) != len(examples):
        raise ValueError(f"{path}: duplicate example ids")
    return examples


def load_selector(provider: str, rules_path: Path | None) -> ToolSelector:
    if provider == "rules":
        if rules_path is None:
            raise ValueError("--provider rules needs --rules")
        raw = yaml.safe_load(rules_path.read_text("utf-8"))
        return RulesToolSelector(ToolRule.model_validate(r) for r in raw)
    module_name, _, attr = provider.partition(":")
    if not attr:
        raise ValueError("--provider is 'rules' or 'package.module:factory'")
    factory = getattr(importlib.import_module(module_name), attr)
    selector: ToolSelector = factory()
    return selector


async def evaluate(
    selector: ToolSelector,
    catalog: tuple[LLMToolDefinition, ...],
    examples: list[Example],
    settings: ToolSelectionSettings,
) -> Report:
    # Always trigger: the evaluation measures the ranking, not the production limits.
    forced = settings.model_copy(update={"max_tools_per_call": 1})
    total_tokens = sum(estimate_tokens(t) for t in catalog)
    rows: list[Row] = []
    removed_tools: list[float] = []
    removed_tokens: list[float] = []
    seen: dict[str, list[int]] = {}  # tool -> [kept, expected]
    for ex in examples:
        monitor = SelectionMonitor("eval", "0")
        decision: dict[str, Any] = await decide(forced, selector, monitor, ex.message, catalog)
        shown_tools = (
            reduce_catalog(forced, catalog, decision["kept"])
            if decision["mode"] == "selected"
            else catalog
        )
        shown = [t.name for t in shown_tools]
        missed = [name for name in ex.expected if name not in shown]
        for name in ex.expected:
            counts = seen.setdefault(name, [0, 0])
            counts[1] += 1
            counts[0] += name in shown
        rows.append(
            Row(
                ex.id,
                ex.message,
                list(ex.expected),
                shown,
                decision["mode"],
                decision["reason"],
                not missed,
                missed,
            )
        )
        removed_tools.append(1 - len(shown) / len(catalog))
        kept_tokens = sum(estimate_tokens(t) for t in shown_tools)
        removed_tokens.append(1 - kept_tokens / total_tokens)
    n = len(rows)
    selected = [r for r in rows if r.mode == "selected"]
    expected_total = sum(c[1] for c in seen.values())
    return Report(
        examples=n,
        hit_rate=sum(r.hit for r in rows) / n,
        hit_rate_selected=(sum(r.hit for r in selected) / len(selected)) if selected else 0.0,
        tool_recall=(sum(c[0] for c in seen.values()) / expected_total) if expected_total else 1.0,
        fallback_rate=sum(r.mode == "full" for r in rows) / n,
        tools_removed=sum(removed_tools) / n,
        tokens_removed=sum(removed_tokens) / n,
        catalog_tools=len(catalog),
        catalog_tokens=total_tokens,
        per_tool_recall={k: v[0] / v[1] for k, v in sorted(seen.items())},
        rows=rows,
    )


def pct(x: float) -> str:
    return f"{x:6.1%}"


def render(report: Report) -> str:
    lines = [
        f"examples: {report.examples}   catalog: {report.catalog_tools} tools, "
        f"~{report.catalog_tokens} tokens",
        f"hit rate (all expected tools kept): {pct(report.hit_rate)}",
        f"  where the filter reduced the list: {pct(report.hit_rate_selected)}",
        f"tool recall (expected occurrences kept): {pct(report.tool_recall)}",
        f"fallback rate (full list sent): {pct(report.fallback_rate)}",
        f"saving: tools removed {pct(report.tools_removed)}, "
        f"tokens removed {pct(report.tokens_removed)}",
        "per-tool recall:",
        *[f"  {name:<24}{pct(r)}" for name, r in report.per_tool_recall.items()],
    ]
    misses = [r for r in report.rows if not r.hit]
    if misses:
        lines.append("misses:")
        lines += [f"  {r.id} {r.message!r}: missing {r.missed} ({r.reason})" for r in misses]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--provider", default="rules")
    parser.add_argument("--rules", type=Path)
    parser.add_argument("--top-n", type=int, default=ToolSelectionSettings().top_n)
    parser.add_argument("--min-score", type=float, default=ToolSelectionSettings().min_score)
    parser.add_argument("--json", type=Path, help="also write the full report here")
    parser.add_argument("--min-hit-rate", type=float, help="exit 1 below this hit rate")
    args = parser.parse_args(argv)

    catalog = load_catalog(args.catalog)
    examples = load_dataset(args.dataset, catalog)
    selector = load_selector(args.provider, args.rules)
    settings = ToolSelectionSettings(top_n=args.top_n, min_score=args.min_score)
    report = asyncio.run(evaluate(selector, catalog, examples, settings))
    print(render(report))
    if args.json:
        args.json.write_text(json.dumps(asdict(report), ensure_ascii=False, indent=2), "utf-8")
    if args.min_hit_rate is not None and report.hit_rate < args.min_hit_rate:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
