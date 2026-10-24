"""Offline evaluation of a tool selector against a labelled dataset (provider-agnostic).

    uv run python -m conversation_agent.app.tool_selection_eval \\
        --catalog examples/tool-selection-eval/catalog.json \\
        --dataset examples/tool-selection-eval/dataset.jsonl \\
        --provider rules --rules examples/tool-selection-eval/rules.yaml

`--provider` is `rules`, `http` (an external server: `--url`, optional `--token-env`), `jev`
(TypeSafe Jev: `--token-env` required, `--url` defaults to https://api.typesafe.ai, `--model`) or
`package.module:factory` (a callable taking no arguments that returns a `ToolSelector`), so a
classifier or embeddings provider is measured with the same files.

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
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml

from conversation_agent.adapters.connections.static import StaticConnectionResolver
from conversation_agent.adapters.secrets.providers import InMemorySecretProvider
from conversation_agent.adapters.tool_selection.http import HttpToolSelector
from conversation_agent.adapters.tool_selection.jev import JevToolSelector
from conversation_agent.adapters.tool_selection.rules import RulesToolSelector
from conversation_agent.adapters.tools.guard import GuardedTransport
from conversation_agent.core.definitions.tool_selection import (
    HttpSelectorSettings,
    JevSelectorSettings,
    ToolRule,
    ToolSelectionSettings,
)
from conversation_agent.core.models.connections import AuthSpec, ResolvedConnection
from conversation_agent.core.models.llm import LLMToolDefinition
from conversation_agent.engine.tool_selection import (
    SelectionMonitor,
    decide,
    estimate_tokens,
    reduce_catalog,
)
from conversation_agent.ports.tool_selector import ToolScore, ToolSelector


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
    tools_kept: float  # mean number of tools shown to the model
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


def http_selector(
    url: str,
    path: str,
    token: str | None,
    top_n: int,
    client: httpx.AsyncClient | None = None,
) -> ToolSelector:
    """The `http` provider against `url` (a development/evaluation connection: private networks
    are allowed, TLS only when the URL says https; in production the OPERATOR registers it)."""
    connection = ResolvedConnection(
        connection_id="eval",
        base_url=url,
        allow_private_networks=True,
        tls_required=url.startswith("https://"),
        auth=AuthSpec(secret_ref="token") if token else None,
    )
    guard = GuardedTransport(
        StaticConnectionResolver({"eval": connection}),
        InMemorySecretProvider({("eval", "token"): token}) if token else None,
        client,
    )
    return HttpToolSelector(guard, HttpSelectorSettings(connection="eval", path=path), top_n=top_n)


def jev_selector(
    url: str, path: str | None, token: str, model: str, top_n: int, timeout: float = 10.0
) -> ToolSelector:
    """The `jev` provider against `url` with the key in `token` (evaluation connection; in
    production the OPERATOR registers it and the key comes from the SecretProvider)."""
    connection = ResolvedConnection(
        connection_id="eval",
        base_url=url,
        allowed_hosts=(httpx.URL(url).host,),
        tls_required=url.startswith("https://"),
        auth=AuthSpec(secret_ref="token"),
    )
    guard = GuardedTransport(
        StaticConnectionResolver({"eval": connection}),
        InMemorySecretProvider({("eval", "token"): token}),
    )
    extra = {"path": path} if path else {}
    settings = JevSelectorSettings(connection="eval", model=model, timeout_seconds=timeout, **extra)
    return JevToolSelector(guard, settings, top_n=top_n)


def load_selector(
    provider: str,
    rules_path: Path | None,
    *,
    url: str | None = None,
    path: str | None = None,
    token: str | None = None,
    top_n: int = 8,
    model: str = "jev-latest",
) -> ToolSelector:
    if provider == "jev":
        if token is None:
            raise ValueError("--provider jev needs --token-env naming a set variable")
        return jev_selector(url or "https://api.typesafe.ai", path, token, model, top_n)
    if provider == "http":
        if url is None:
            raise ValueError("--provider http needs --url")
        return http_selector(url, path or "/tool-selection", token, top_n)
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
        decision: dict[str, Any] = await decide(
            forced, selector, monitor, ex.message, catalog, tenant_id="eval"
        )
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
        tools_kept=sum(len(r.shown) for r in rows) / n,
        catalog_tools=len(catalog),
        catalog_tokens=total_tokens,
        per_tool_recall={k: v[0] / v[1] for k, v in sorted(seen.items())},
        rows=rows,
    )


class MemoSelector:
    """Remembers each answer so a sweep over `min_score` asks the provider once per message."""

    def __init__(self, inner: ToolSelector) -> None:
        self._inner = inner
        self._seen: dict[str, list[ToolScore]] = {}

    async def rank(
        self, message: str, candidates: tuple[LLMToolDefinition, ...], *, tenant_id: str
    ) -> list[ToolScore]:
        if message not in self._seen:
            self._seen[message] = await self._inner.rank(message, candidates, tenant_id=tenant_id)
        return self._seen[message]


async def sweep(
    selector: ToolSelector,
    catalog: tuple[LLMToolDefinition, ...],
    examples: list[Example],
    settings: ToolSelectionSettings,
    min_scores: list[float],
) -> list[tuple[float, Report]]:
    memo = MemoSelector(selector)
    return [
        (ms, await evaluate(memo, catalog, examples, settings.model_copy(update={"min_score": ms})))
        for ms in min_scores
    ]


def render_sweep(results: list[tuple[float, Report]]) -> str:
    lines = ["min_score   hit rate   recall   fallback   shown (mean)   tools removed"]
    for ms, r in results:
        lines.append(
            f"{ms:9.2f}  {pct(r.hit_rate)}  {pct(r.tool_recall)}  {pct(r.fallback_rate)}  "
            f"{r.tools_kept:12.2f}  {pct(r.tools_removed)}"
        )
    return "\n".join(lines)


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
        f"tools shown to the model (mean): {report.tools_kept:.2f} of {report.catalog_tools}",
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
    parser.add_argument("--url", help="base URL of the external server (--provider http)")
    parser.add_argument("--path", help="endpoint path (default: the provider's own)")
    parser.add_argument("--model", default="jev-latest", help="Jev model (--provider jev)")
    parser.add_argument("--token-env", help="env var holding a bearer token for the server")
    parser.add_argument("--top-n", type=int, default=ToolSelectionSettings().top_n)
    parser.add_argument("--min-score", type=float, default=ToolSelectionSettings().min_score)
    parser.add_argument(
        "--sweep",
        help="comma-separated min_score values (e.g. 0.05,0.1,0.15,0.25): one provider call per "
        "message, one table row per value",
    )
    parser.add_argument("--json", type=Path, help="also write the full report here")
    parser.add_argument("--min-hit-rate", type=float, help="exit 1 below this hit rate")
    args = parser.parse_args(argv)

    catalog = load_catalog(args.catalog)
    examples = load_dataset(args.dataset, catalog)
    token = os.environ.get(args.token_env) if args.token_env else None
    selector = load_selector(
        args.provider,
        args.rules,
        url=args.url,
        path=args.path,
        token=token,
        top_n=args.top_n,
        model=args.model,
    )
    settings = ToolSelectionSettings(top_n=args.top_n, min_score=args.min_score)
    if args.sweep:
        values = [float(v) for v in args.sweep.split(",")]
        print(render_sweep(asyncio.run(sweep(selector, catalog, examples, settings, values))))
        return 0
    report = asyncio.run(evaluate(selector, catalog, examples, settings))
    print(render(report))
    if args.json:
        args.json.write_text(json.dumps(asdict(report), ensure_ascii=False, indent=2), "utf-8")
    if args.min_hit_rate is not None and report.hit_rate < args.min_hit_rate:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
